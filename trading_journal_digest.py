"""
Monthly trading journal statistics -> Telegram bot.

This is a SEPARATE script/workflow from the others in this repo — sends
its own message, on its own schedule, to the same Telegram bot.

What this does:
1. Downloads your trading journal as CSV from a Google Sheet (published
   via a plain "anyone with the link can view" share, exported as CSV —
   no Google API key or OAuth needed, see README for setup).
2. Filters rows to the previous calendar month.
3. Computes win rate and average R broken down by: overall, instrument,
   day of week, rule-based vs discretionary, CFTC-consensus-aligned vs
   not, and by setup tag (only tags with enough samples to mean
   anything).
4. Sends a Russian-language summary to Telegram — no AI involved, this
   is plain arithmetic on your own data.

Expected Google Sheet columns (header row, in this order or any order —
matched by name, not position):
    Date | Instrument | Direction | Result_R | Rule_Based | CFTC_Aligned | Setup | Notes

    Date          - YYYY-MM-DD (or most common date formats)
    Instrument    - e.g. EURUSD, XAUUSD (free text)
    Direction     - Long / Short (not currently used in stats, but kept
                    for your own reference)
    Result_R      - numeric: your result in R-multiples (or % — just be
                    consistent). Positive = win, negative = loss, 0 = BE.
    Rule_Based    - Да / Нет (was this a planned, system trade)
    CFTC_Aligned  - Да / Нет / Не проверял (did direction match CFTC
                    consensus from the weekly report at entry time)
    Setup         - free-text tag (breakout, reversal, news, etc.)
    Notes         - free text, not used in stats

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests

Environment variables required:
    TELEGRAM_BOT_TOKEN     - same one already used by the other scripts
    TELEGRAM_CHAT_ID       - same one already used by the other scripts
    JOURNAL_SHEET_CSV_URL  - the CSV export URL of your Google Sheet
                             (see README for how to get this)

NOTE: unlike the other scripts in this repo, this one needs no state
file — each run freshly computes stats for "last calendar month" from
whatever is in the sheet at run time, so there's nothing to persist
between runs.
"""

import csv
import io
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SHEET_CSV_URL = os.environ.get("JOURNAL_SHEET_CSV_URL")

MIN_SAMPLES_FOR_BREAKDOWN = 3  # don't report a bucket's stats on fewer trades than this

DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y")


# ---------------------------------------------------------------------
# Fetching + parsing the sheet
# ---------------------------------------------------------------------

def fetch_journal_rows() -> list:
    if not SHEET_CSV_URL:
        raise RuntimeError("JOURNAL_SHEET_CSV_URL is not set.")

    resp = requests.get(SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not row.get("Date"):
            continue
        parsed_date = _parse_date(row["Date"])
        if parsed_date is None:
            print(f"Warning: skipping row with unparseable date: {row.get('Date')!r}", file=sys.stderr)
            continue
        try:
            result_r = float(row.get("Result_R", "").replace(",", "."))
        except ValueError:
            print(f"Warning: skipping row with unparseable Result_R: {row.get('Result_R')!r}", file=sys.stderr)
            continue

        rows.append({
            "date": parsed_date,
            "instrument": row.get("Instrument", "").strip() or "?",
            "direction": row.get("Direction", "").strip(),
            "result_r": result_r,
            "rule_based": row.get("Rule_Based", "").strip().lower(),
            "cftc_aligned": row.get("CFTC_Aligned", "").strip().lower(),
            "setup": row.get("Setup", "").strip() or "(без тега)",
        })
    return rows


def _parse_date(text: str):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------

def previous_month_bounds(today: date) -> tuple:
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    return first_of_prev_month, last_of_prev_month


def _bucket_stats(trades: list) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["result_r"] > 0)
    total_r = sum(t["result_r"] for t in trades)
    avg_r = total_r / n if n else 0.0
    win_rate = 100 * wins / n if n else 0.0
    return {"n": n, "wins": wins, "win_rate": win_rate, "total_r": total_r, "avg_r": avg_r}


def compute_breakdown(trades: list, key_func) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[key_func(t)].append(t)
    return {k: _bucket_stats(v) for k, v in groups.items() if v}


RUSSIAN_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


# ---------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------

def fmt_bucket(label: str, stats: dict) -> str:
    if stats["n"] < MIN_SAMPLES_FOR_BREAKDOWN:
        return f"  {label}: недостаточно данных ({stats['n']} сделок)"
    sign = "+" if stats["total_r"] >= 0 else ""
    return (
        f"  {label}: {stats['n']} сделок, винрейт {stats['win_rate']:.0f}%, "
        f"сумма {sign}{stats['total_r']:.1f}R, среднее {stats['avg_r']:+.2f}R"
    )


def build_message(trades: list, period_start: date, period_end: date) -> str:
    period_label = f"{period_start.strftime('%d.%m.%Y')} — {period_end.strftime('%d.%m.%Y')}"
    lines = [f"\U0001F4D2 Статистика дневника трейдера за период {period_label}\n"]

    if not trades:
        lines.append("За этот период сделок в таблице не найдено.")
        return "\n".join(lines)

    overall = _bucket_stats(trades)
    sign = "+" if overall["total_r"] >= 0 else ""
    lines.append(
        f"Всего сделок: {overall['n']}\n"
        f"Винрейт: {overall['win_rate']:.0f}%\n"
        f"Суммарный результат: {sign}{overall['total_r']:.1f}R\n"
        f"Средний результат на сделку: {overall['avg_r']:+.2f}R\n"
    )

    lines.append("\U0001F4C8 По инструментам:")
    by_instrument = compute_breakdown(trades, lambda t: t["instrument"])
    for instrument, stats in sorted(by_instrument.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(fmt_bucket(instrument, stats))
    lines.append("")

    lines.append("\U0001F4C5 По дням недели:")
    by_weekday = compute_breakdown(trades, lambda t: RUSSIAN_WEEKDAYS[t["date"].weekday()])
    for wd in RUSSIAN_WEEKDAYS:
        if wd in by_weekday:
            lines.append(fmt_bucket(wd, by_weekday[wd]))
    lines.append("")

    lines.append("\U0001F4CB По системе vs дискреция:")
    by_rule = compute_breakdown(trades, lambda t: "По системе" if t["rule_based"] == "да" else "Дискреция" if t["rule_based"] == "нет" else "Не указано")
    for label in ("По системе", "Дискреция", "Не указано"):
        if label in by_rule:
            lines.append(fmt_bucket(label, by_rule[label]))
    lines.append("")

    cftc_trades = [t for t in trades if t["cftc_aligned"] in ("да", "нет")]
    if cftc_trades:
        lines.append("\U0001F3AF Совпадение с консенсусом CFTC:")
        by_cftc = compute_breakdown(cftc_trades, lambda t: "Совпадало" if t["cftc_aligned"] == "да" else "Не совпадало")
        for label in ("Совпадало", "Не совпадало"):
            if label in by_cftc:
                lines.append(fmt_bucket(label, by_cftc[label]))
        lines.append("")

    lines.append("\U0001F3F7 По тегам сетапа:")
    by_setup = compute_breakdown(trades, lambda t: t["setup"])
    setup_items = sorted(by_setup.items(), key=lambda kv: -kv[1]["n"])
    shown_any = False
    for setup, stats in setup_items:
        if stats["n"] >= MIN_SAMPLES_FOR_BREAKDOWN:
            lines.append(fmt_bucket(setup, stats))
            shown_any = True
    if not shown_any:
        lines.append(f"  Пока нет тегов с {MIN_SAMPLES_FOR_BREAKDOWN}+ сделками для сравнения.")

    lines.append(
        "\n\u26A0\uFE0F Статистика считается по вашим собственным данным из таблицы, "
        "без каких-либо внешних сигналов — чем больше сделок, тем надёжнее выводы."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    today = datetime.now(timezone.utc).date()
    period_start, period_end = previous_month_bounds(today)

    all_rows = fetch_journal_rows()
    trades = [r for r in all_rows if period_start <= r["date"] <= period_end]

    print(f"Diagnostic: {len(all_rows)} total rows in sheet, {len(trades)} trades in {period_start}..{period_end}.")

    message = build_message(trades, period_start, period_end)
    send_telegram_message(message)
    print("Journal stats report sent.")


if __name__ == "__main__":
    main()
