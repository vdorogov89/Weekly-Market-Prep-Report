"""
Monthly top-10 US stock gainers report (trailing 3 months) -> Telegram bot.

This is a SEPARATE script/workflow from sentiment_report.py (CFTC report)
and weekly_market_prep.py (calendar + ranges) — sends its own message, on
its own schedule, to the same Telegram bot. Nothing in the other two
scripts is touched.

What this does:
1. Uses a fixed WATCHLIST of tickers (defined below — NOT the full S&P
   500 anymore; edit WATCHLIST directly to change which stocks are
   considered).
2. Uses a ROLLING 3-month window ending yesterday (not calendar-quarter
   boundaries) — so every month's report reflects "the last 3 months",
   updated monthly.
3. Fetches each stock's close price at the start and end of that window
   via Twelve Data's time_series endpoint — same TWELVE_DATA_API_KEY
   already used by weekly_market_prep.py, no new key needed. With a
   watchlist this small (vs. the full S&P 500 previously), the whole
   run takes a few minutes, not hours.
4. Computes each stock's % price change over that window and sends the
   top (up to TOP_N) gainers, ranked, to Telegram.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests python-dateutil

Environment variables required:
    TELEGRAM_BOT_TOKEN    - same one already used by the other scripts
    TELEGRAM_CHAT_ID      - same one already used by the other scripts
    TWELVE_DATA_API_KEY   - same one already used by weekly_market_prep.py

NOTE ON RELIABILITY:
- Twelve Data's free Basic plan is documented as 8 API credits/minute,
  800/day (each symbol in a request costs 1 credit) — but in practice
  the per-minute window can throttle even at exactly 8/8 credits with a
  65s gap (no headroom, and the window isn't perfectly aligned with our
  own timing). This script uses 6 symbols/batch with a 75s gap and
  escalating backoff (75s/120s/180s) on repeated 429s. With only ~31
  symbols in WATCHLIST that's just a handful of batches (a few minutes
  total), not the 1.5-2 hours this took when it covered the full S&P 500.
- This report only makes sense for a stock that had price data for the
  entire 3-month window and still trades under the same ticker; a stock
  that was delisted or renamed partway through is simply skipped (not
  enough data points), not counted as an error.
"""

import json
import os
import sys
import time
from datetime import date, timedelta, datetime, timezone

import requests
from dateutil.relativedelta import relativedelta

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_API_KEY")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

WINDOW_MONTHS = 3
# Twelve Data's free Basic plan allows 120 symbols per batch call, but the
# real constraint is the free plan's rate limit: 8 API CREDITS per minute
# (each symbol in a call costs 1 credit), 800/day. A batch of 100 symbols
# would request 100 credits at once and immediately hit that per-minute
# cap. Even at exactly 8 symbols/batch with a 65s gap, the free plan can
# still throttle intermittently (the per-minute window isn't perfectly
# aligned with our own timing, and there's zero headroom at exactly 8/8
# credits) — so this uses a smaller batch with real headroom, a longer
# gap, and escalating backoff on repeated 429s.
BATCH_SIZE = 6
BATCH_DELAY_SECONDS = 75  # comfortably over a minute, with headroom below the 8-credit cap
TOP_N = 10

# Fixed watchlist — edit this dict directly to add, remove, or rename
# tickers. {symbol: display_name}. Twelve Data uses '-' for share
# classes (e.g. BRK-B), not '.'.
WATCHLIST = {
    "LIN": "Linde",
    "SHW": "Sherwin-Williams",
    "FCX": "Freeport-McMoRan",
    "GOOG": "Alphabet",
    "TMUS": "T-Mobile US",
    "XOM": "Exxon Mobil",
    "CVX": "Chevron",
    "BRK-B": "Berkshire Hathaway",
    "JPM": "JPMorgan Chase",
    "BAC": "Bank of America",
    "UNP": "Union Pacific",
    "UPS": "United Parcel Service",
    "RTX": "RTX Corporation",
    "HON": "Honeywell",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "PG": "Procter & Gamble",
    "KO": "Coca-Cola",
    "PEP": "PepsiCo",
    "COST": "Costco",
    "PLD": "Prologis",
    "AMT": "American Tower",
    "CCI": "Crown Castle",
    "NEE": "NextEra Energy",
    "DUK": "Duke Energy",
    "SO": "Southern Company",
    "D": "Dominion Energy",
    "UNH": "UnitedHealth Group",
    "JNJ": "Johnson & Johnson",
    "PFE": "Pfizer",
    "AMZN": "Amazon",
}


def fetch_sp500_list() -> dict:
    """Returns {symbol: display_name} — now just the fixed WATCHLIST
    above (kept as a function for minimal disruption to the rest of
    the script, which calls this to get its ticker universe)."""
    return dict(WATCHLIST)


# ---------------------------------------------------------------------
# Rolling window boundaries
# ---------------------------------------------------------------------

def rolling_window_bounds(today: date) -> tuple:
    """
    Returns (start_date, end_date) for a rolling WINDOW_MONTHS-month
    window ending yesterday (yesterday, rather than today, so we don't
    ask for a trading day that may not have closed/settled data yet).
    """
    end_date = today - timedelta(days=1)
    start_date = end_date - relativedelta(months=WINDOW_MONTHS)
    return start_date, end_date


# ---------------------------------------------------------------------
# Price data (Twelve Data, batched)
# ---------------------------------------------------------------------

def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_batch_with_retry(batch: list, start_date: date, end_date: date, max_retries: int = 3) -> dict:
    """
    Fetches one batch, retrying on a 429 (rate limit) with an escalating
    wait (75s, 120s, 180s) — the free plan's per-minute window can be
    stricter in practice than the documented 8 credits/minute, so this
    gives increasing headroom on repeated failures rather than retrying
    at a fixed interval that keeps landing in the same throttled window.
    """
    params = {
        "symbol": ",".join(batch),
        "interval": "1day",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "apikey": TWELVE_DATA_KEY,
    }
    backoffs = [75, 120, 180]
    for attempt in range(max_retries + 1):
        resp = requests.get(TWELVE_DATA_URL, params=params, timeout=60)
        if resp.status_code == 429 and attempt < max_retries:
            wait_seconds = backoffs[min(attempt, len(backoffs) - 1)]
            # Print the response body once, on the first failure, so the
            # exact reason from Twelve Data (daily cap vs per-minute vs
            # something else) is visible in the workflow log if this
            # keeps happening.
            if attempt == 0:
                print(f"Rate limited (429). Response: {resp.text[:300]}", file=sys.stderr)
            print(f"Waiting {wait_seconds}s and retrying...", file=sys.stderr)
            time.sleep(wait_seconds)
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


def fetch_window_returns(symbols: list, start_date: date, end_date: date) -> dict:
    """
    Returns {symbol: pct_change} for every symbol that had usable price
    data for both the start and end of the window. Symbols with missing
    or unusable data are silently omitted (not an error — e.g. a stock
    added to the index partway through the window).

    Runs at BATCH_SIZE symbols per call with a BATCH_DELAY_SECONDS pause
    between calls (see the constants above for why) — for the full S&P
    500 list this takes roughly an hour end to end, which is expected
    and fine for a job that only runs once a month.
    """
    if not TWELVE_DATA_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not set.")

    returns = {}
    batches = list(chunked(symbols, BATCH_SIZE))

    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(BATCH_DELAY_SECONDS)

        try:
            data = _fetch_batch_with_retry(batch, start_date, end_date)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: batch {i+1}/{len(batches)} request failed: {exc}", file=sys.stderr)
            continue

        # Single-symbol requests return one object directly; batch
        # requests return {symbol: {...}, symbol: {...}, ...}. Since we
        # always request more than one symbol here, we expect the batch
        # (dict-of-dicts) shape.
        for symbol in batch:
            entry = data.get(symbol)
            if not entry or "values" not in entry:
                continue
            values = entry["values"]
            if len(values) < 2:
                continue
            # Twelve Data returns newest-first by default; sort explicitly.
            values_sorted = sorted(values, key=lambda v: v["datetime"])
            try:
                first_close = float(values_sorted[0]["close"])
                last_close = float(values_sorted[-1]["close"])
                if first_close <= 0:
                    continue
                pct_change = (last_close / first_close - 1) * 100
                returns[symbol] = pct_change
            except (KeyError, ValueError):
                continue

    return returns


# ---------------------------------------------------------------------
# Message assembly + sending
# ---------------------------------------------------------------------

TOP10_STATE_FILE = "top10_companies.json"


def compute_top10(companies: dict, start_date: date, end_date: date) -> tuple:
    """
    Returns (top_list, analyzed_count, total_count) where top_list is a
    list of {"rank", "symbol", "name", "pct_change"} dicts, longest
    first. analyzed_count/total_count are for the "N of M analyzed"
    footer line.
    """
    symbols = list(companies.keys())
    returns = fetch_window_returns(symbols, start_date, end_date)

    ranked = sorted(returns.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    top_list = [
        {"rank": i, "symbol": symbol, "name": companies.get(symbol, symbol), "pct_change": pct}
        for i, (symbol, pct) in enumerate(ranked, start=1)
    ]
    return top_list, len(returns), len(symbols)


def save_top10_state(top_list: list, report_date: date, window_start: date, window_end: date) -> None:
    """
    Saves the top-10 list to a JSON file so a separate daily script
    (company_deepdive.py) can send one deep-dive report per company on
    the days following this report, in rank order.
    """
    data = {
        "report_date": report_date.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "companies": top_list,
    }
    with open(TOP10_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_message(top_list: list, analyzed_count: int, total_count: int, start_date: date, end_date: date) -> str:
    header = (
        f"\U0001F3C6 Топ-10 акций S&P 500 по росту цены за последние {WINDOW_MONTHS} месяца\n"
        f"({start_date.strftime('%d.%m.%Y')} — {end_date.strftime('%d.%m.%Y')})\n"
    )

    if not top_list:
        return header + "\nНе удалось получить данные — см. лог workflow."

    lines = [header]
    for item in top_list:
        sign = "+" if item["pct_change"] >= 0 else ""
        lines.append(f"{item['rank']}. {item['symbol']} ({item['name']}): {sign}{item['pct_change']:.1f}%")

    lines.append(
        f"\nПроанализировано инструментов: {analyzed_count} из {total_count} "
        f"(остальные пропущены из-за нехватки данных, например недавние IPO)."
    )
    lines.append(
        "\u26A0\uFE0F Рост цены за прошедший период — исторический факт, "
        "не прогноз и не рекомендация."
    )
    lines.append(
        "\n\U0001F4C4 С завтрашнего дня — подробный разбор каждой компании "
        "из этого списка, по одной в день."
    )
    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set. "
            "Set them as GitHub repo secrets (see README)."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=20)
    resp.raise_for_status()


def main() -> None:
    today = datetime.now(timezone.utc).date()
    start_date, end_date = rolling_window_bounds(today)

    companies = fetch_sp500_list()
    top_list, analyzed_count, total_count = compute_top10(companies, start_date, end_date)

    if top_list:
        save_top10_state(top_list, today, start_date, end_date)

    message = build_message(top_list, analyzed_count, total_count, start_date, end_date)
    send_telegram_message(message)
    print("Monthly top gainers report sent.")


if __name__ == "__main__":
    main()
