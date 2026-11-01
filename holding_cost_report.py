"""
Weekly report: what it costs to keep your open positions (swap).

Reads the same journal sheet as trading_journal_digest.py (secret
JOURNAL_SHEET_CSV_URL) and sends one Telegram message:

  - for every open position (a row without Close_Date): swap accrued so far
    and swap accrued SINCE THE PREVIOUS REPORT, which is the real cost of
    the week;
  - positions closed during the last 7 days with their swap;
  - total and a rough monthly projection.

The previous week's swap values are kept in a small state file
(holding_cost_state.json). In GitHub Actions the file lives in the Actions
cache, so no trading data is ever committed to the repository.
On the very first run there is no previous value, so the weekly cost is
estimated from the average pace and marked with "≈".

Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JOURNAL_SHEET_CSV_URL  (required)
  STATE_FILE   path of the state file (default holding_cost_state.json)
  DRY_RUN=1    print the message instead of sending it, state is not saved
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import trading_journal_digest as digest

STATE_FILE = os.environ.get("STATE_FILE", "holding_cost_state.json")
WEEKS_PER_MONTH = 4.33


def position_key(pos: dict) -> str:
    return f"{pos['instrument']}|{pos['open_date'].isoformat()}"


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(today: date, positions: list) -> None:
    data = {
        "date": today.isoformat(),
        "swaps": {position_key(p): p["swap"] for p in positions},
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def money(value: float) -> str:
    return f"{value:+,.2f} $".replace(",", " ")


def build_report(today: date, open_positions: list, closed_recent: list, state: dict) -> str:
    prev_date = None
    prev_swaps = {}
    try:
        prev_date = date.fromisoformat(state.get("date", ""))
        prev_swaps = state.get("swaps", {}) or {}
    except ValueError:
        pass

    lines = [f"\U0001F4B8 Стоимость удержания позиций на {today.strftime('%d.%m.%Y')}\n"]

    week_total = 0.0
    any_estimate = False
    if open_positions:
        period_days = (today - prev_date).days if prev_date else None
        if period_days:
            lines.append(f"Открытые позиции (за {period_days} дн. с прошлого отчёта):")
        else:
            lines.append("Открытые позиции:")
        rows = []
        for p in open_positions:
            key = position_key(p)
            if key in prev_swaps:
                week = p["swap"] - float(prev_swaps[key])
                estimate = False
            else:
                days = max((today - p["open_date"]).days, 1)
                week = p["swap"] / days * 7
                estimate = True
                any_estimate = True
            week_total += week
            rows.append((week, p, estimate))
        for week, p, estimate in sorted(rows, key=lambda r: r[0]):
            mark = "≈ " if estimate else ""
            lines.append(
                f"  {p['instrument']} с {p['open_date'].strftime('%d.%m')}: "
                f"{mark}{money(week)} за неделю, всего {money(p['swap'])}"
            )
        total_accrued = sum(p["swap"] for p in open_positions)
        lines.append(f"  Итого за неделю: {'≈ ' if any_estimate else ''}{money(week_total)}")
        lines.append(f"  Накоплено по открытым: {money(total_accrued)}")
        lines.append(f"  Прогноз на месяц при тех же позициях: ≈ {money(week_total * WEEKS_PER_MONTH)}")
        lines.append("")
    else:
        lines.append("Открытых позиций со свопом нет.\n")

    if closed_recent:
        lines.append("Закрыто за последние 7 дней (своп сделки):")
        for t in closed_recent:
            lines.append(f"  {t['instrument']} {t['close_date'].strftime('%d.%m')}: {money(t['swap'])}")
        lines.append(f"  Итого: {money(sum(t['swap'] for t in closed_recent))}")
        lines.append("")

    if any_estimate:
        lines.append("≈ — первая оценка по среднему темпу с открытия; со следующей недели считается точная разница.")
    lines.append(
        "Напоминание: у акций-CFD ставка 0,055% за ночь с обеих сторон (около 20% годовых), "
        "со среды на четверг начисляется втрое."
    )
    return "\n".join(lines)


def main() -> None:
    today = datetime.now(timezone.utc).date()
    all_rows, _skipped = digest.fetch_journal_rows()
    open_positions = list(digest.OPEN_POSITIONS)
    week_ago = today - timedelta(days=7)
    closed_recent = [r for r in all_rows if r["close_date"] >= week_ago and r.get("swap") is not None]

    state = load_state()
    message = build_report(today, open_positions, closed_recent, state)

    if os.environ.get("DRY_RUN"):
        print(message)
        return

    digest.send_telegram_message(message)
    save_state(today, open_positions)
    print("Holding cost report sent.")


if __name__ == "__main__":
    sys.exit(main())
