"""
Weekly market-prep report -> Telegram bot.

This is a SEPARATE script/workflow from sentiment_report.py (the CFTC
positioning report) — it sends its own message, on its own schedule,
to the same Telegram bot. Nothing in sentiment_report.py is touched.

What this sends:
1. Economic calendar for USD (High/Medium impact events for the current
   week) — from Forex Factory's free public JSON feed.
2. Average weekly range for EURUSD and XAUUSD (like a weekly ATR) —
   from Alpha Vantage's free FX_WEEKLY data, averaged over the last 8
   completed weeks.
3. Key levels for the upcoming week (last week's high/low, plus classic
   pivot points P/R1/R2/S1/S2) for EURUSD and XAUUSD — computed from
   the same Alpha Vantage weekly data.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests python-dateutil

Environment variables required:
    TELEGRAM_BOT_TOKEN     - token from @BotFather (same one already used
                              by sentiment_report.py)
    TELEGRAM_CHAT_ID       - your chat id (same one already used)
    ALPHA_VANTAGE_API_KEY  - free key from alphavantage.co (see README)

NOTE ON RELIABILITY:
- The Forex Factory feed is community-relied-upon and free, but
  unofficial; if it goes down or changes its field names, the calendar
  section will show "no data" rather than breaking the whole report.
- Alpha Vantage's free tier is 25 requests/day, 5/minute — this script
  only makes 2 requests per run, so that's not a concern.
"""

import os
import statistics
import sys
from datetime import datetime, timezone

import requests
from dateutil import parser as date_parser

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# label -> (from_symbol, to_symbol) for Alpha Vantage FX_WEEKLY
INSTRUMENTS = {
    "EURUSD": ("EUR", "USD"),
    "XAUUSD": ("XAU", "USD"),
}

WEEKS_FOR_AVERAGE = 8


# ---------------------------------------------------------------------
# Economic calendar (USD)
# ---------------------------------------------------------------------

def fetch_usd_calendar() -> list:
    """
    Returns a list of {"title", "when", "impact"} dicts for this week's
    USD High/Medium impact events, sorted by time. Returns [] (with a
    warning printed) if the feed can't be fetched or parsed.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WeeklyPrepBot/1.0)"}
    try:
        resp = requests.get(CALENDAR_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        raw_events = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to fetch economic calendar: {exc}", file=sys.stderr)
        return []

    events = []
    for e in raw_events:
        try:
            if e.get("country") != "USD":
                continue
            if e.get("impact") not in ("High", "Medium"):
                continue
            when = date_parser.parse(e["date"])
            events.append({"title": e.get("title", "?"), "when": when, "impact": e["impact"]})
        except Exception:  # noqa: BLE001
            continue  # skip malformed rows rather than failing the whole feed

    events.sort(key=lambda ev: ev["when"])
    return events


def format_calendar_section(events: list) -> str:
    if not events:
        return "Экономический календарь (USD): нет данных или на этой неделе нет важных событий."

    lines = ["\U0001F5D3 Экономический календарь (USD, High/Medium impact):"]
    for ev in events:
        impact_icon = "\U0001F534" if ev["impact"] == "High" else "\U0001F7E1"
        when_str = ev["when"].strftime("%a %d.%m %H:%M")
        lines.append(f"{impact_icon} {when_str} — {ev['title']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Weekly range + key levels (Alpha Vantage)
# ---------------------------------------------------------------------

def fetch_weekly_bars(from_symbol: str, to_symbol: str) -> list:
    """
    Returns a list of (date_str, open, high, low, close) tuples, sorted
    newest first. Raises RuntimeError if Alpha Vantage returns no usable
    data (bad key, rate limit, unsupported symbol, etc).
    """
    if not ALPHA_VANTAGE_KEY:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY is not set.")

    params = {
        "function": "FX_WEEKLY",
        "from_symbol": from_symbol,
        "to_symbol": to_symbol,
        "apikey": ALPHA_VANTAGE_KEY,
    }
    resp = requests.get(ALPHA_VANTAGE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    series = data.get("Time Series FX (Weekly)")
    if not series:
        # Alpha Vantage returns a "Note"/"Information" field instead of
        # data when the key is invalid or a rate limit is hit.
        reason = data.get("Note") or data.get("Information") or data.get("Error Message") or str(data)[:200]
        raise RuntimeError(f"No weekly series for {from_symbol}/{to_symbol}: {reason}")

    bars = []
    for date_str in sorted(series.keys(), reverse=True):
        row = series[date_str]
        bars.append((
            date_str,
            float(row["1. open"]),
            float(row["2. high"]),
            float(row["3. low"]),
            float(row["4. close"]),
        ))
    return bars


def compute_stats(bars: list) -> dict:
    """
    bars: newest-first list of (date, open, high, low, close).
    Skips index 0 (the current, possibly still-open week) and computes:
      - last_completed_week: the most recently fully closed week's bar
      - avg_range: average (high-low) over the last WEEKS_FOR_AVERAGE
        completed weeks
      - pivot levels for the upcoming week, based on the last completed
        week's high/low/close (classic floor-trader pivots)
    """
    completed = bars[1:]
    if not completed:
        raise RuntimeError("Not enough weekly bars to compute stats.")

    date_str, o, h, l, c = completed[0]
    ranges = [(bh - bl) for (_, bo, bh, bl, bc) in completed[:WEEKS_FOR_AVERAGE]]
    avg_range = statistics.mean(ranges)

    pivot = (h + l + c) / 3
    r1 = 2 * pivot - l
    s1 = 2 * pivot - h
    r2 = pivot + (h - l)
    s2 = pivot - (h - l)

    return {
        "week_of": date_str,
        "last_week_high": h,
        "last_week_low": l,
        "last_week_close": c,
        "last_week_range": h - l,
        "avg_range": avg_range,
        "weeks_averaged": len(ranges),
        "pivot": pivot,
        "r1": r1,
        "r2": r2,
        "s1": s1,
        "s2": s2,
    }


def format_levels_section(label: str, stats: dict, decimals: int) -> str:
    fmt = f"{{:.{decimals}f}}"
    return (
        f"\U0001F4CF {label} (по данным на {stats['week_of']}):\n"
        f"  Средний недельный диапазон ({stats['weeks_averaged']} нед.): "
        f"{fmt.format(stats['avg_range'])}\n"
        f"  Прошлая неделя: High {fmt.format(stats['last_week_high'])} / "
        f"Low {fmt.format(stats['last_week_low'])} "
        f"(диапазон {fmt.format(stats['last_week_range'])})\n"
        f"  Пивоты на неделю: R2 {fmt.format(stats['r2'])} | R1 {fmt.format(stats['r1'])} | "
        f"P {fmt.format(stats['pivot'])} | S1 {fmt.format(stats['s1'])} | S2 {fmt.format(stats['s2'])}"
    )


# ---------------------------------------------------------------------
# Message assembly + sending
# ---------------------------------------------------------------------

def build_message() -> str:
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    lines = [f"\U0001F9ED Подготовка к неделе — {today} (UTC)\n"]

    calendar_events = fetch_usd_calendar()
    lines.append(format_calendar_section(calendar_events))
    lines.append("")

    # EURUSD is typically quoted to 4-5 decimals, XAUUSD to 2.
    decimals_by_label = {"EURUSD": 5, "XAUUSD": 2}

    for label, (from_symbol, to_symbol) in INSTRUMENTS.items():
        try:
            bars = fetch_weekly_bars(from_symbol, to_symbol)
            stats = compute_stats(bars)
            lines.append(format_levels_section(label, stats, decimals_by_label.get(label, 4)))
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to compute levels for {label}: {exc}", file=sys.stderr)
            lines.append(f"\U0001F4CF {label}: нет данных (см. лог workflow).")
        lines.append("")

    lines.append(
        "\u26A0\uFE0F Диапазоны и пивоты — расчётные ориентиры на основе прошлой недели, "
        "не торговая рекомендация."
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
    message = build_message()
    send_telegram_message(message)
    print("Weekly market-prep report sent.")


if __name__ == "__main__":
    main()
