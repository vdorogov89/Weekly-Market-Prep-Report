"""
Monthly trading journal statistics -> Telegram bot.

This is a SEPARATE script/workflow from the others in this repo — sends
its own message, on its own schedule, to the same Telegram bot.

What this does:
1. Downloads your trading journal as CSV from a Google Sheet (published
   via a plain "anyone with the link can view" share, exported as CSV —
   no Google API key or OAuth needed, see README for setup).
2. Filters rows to trades CLOSED in the previous calendar month (by
   Close_Date — that's when a trade's result is realized).
3. Computes win rate and average R broken down by: overall, instrument,
   day of week, rule-based vs discretionary, CFTC-consensus-aligned vs
   not, setup tag, and holding period (intraday / swing / position).
4. Sends a Russian-language summary to Telegram — no AI involved, this
   is plain arithmetic on your own data.

Expected Google Sheet columns (header row, in this order or any order —
matched by name, not position):
    Open_Date | Close_Date | Instrument | Direction | Open_Price | Close_Price | Result_R | Rule_Based | CFTC_Aligned | State | Setup | Notes | Open_Time | Close_Time | Stop_Price | Target_Price | Exit_Reason

    (The last five are optional and can simply be appended after Notes.)

    Open_Date     - date you entered the trade, YYYY-MM-DD (or common formats)
    Close_Date    - date you closed the trade, same format. Required —
                    this is what "last month" filtering uses, and it's
                    also what makes a specific trade findable later when
                    you ask about it (Claude can locate it by date/price
                    instead of having to guess which trade you mean).
    Instrument    - e.g. EURUSD, XAUUSD (free text)
    Direction     - Long / Short (for reference, not used in stats)
    Open_Price    - price you entered at (optional but recommended — lets
                    a specific trade be cross-checked against real price
                    history later, e.g. via the weekly range/levels report)
    Close_Price   - price you exited at (same as above, optional)
    Result_R      - numeric: your result in R-multiples (or % — just be
                    consistent). Positive = win, negative = loss, 0 = BE.
    Rule_Based    - Да / Нет (was this a planned, system trade)
    CFTC_Aligned  - Да / Нет / Не проверял (did direction match CFTC
                    consensus from the weekly report at entry time)
    State         - your state at entry, one word: Усталость / Спокойствие /
                    Азарт (optional; blank is fine — such trades are simply
                    left out of the by-state breakdown)
    Setup         - free-text tag (breakout, reversal, news, etc.)
    Notes         - free text, not used in stats
    Open_Time /   - HH:MM, in one consistent timezone (e.g. your terminal's).
    Close_Time      Optional. Used only to find a trade again later and to
                    catch typos (close time earlier than open time).
    Stop_Price    - your planned stop at entry. Optional, but this is what
                    lets the report compare planned risk/reward with what
                    you actually got, and check that R matches the prices.
    Target_Price  - your planned target at entry. Optional.
    Exit_Reason   - Стоп / Тейк / Вручную / По времени (optional): why the
                    trade actually ended.

Data checks: every run also validates the rows it reads (close before open,
R sign contradicting the price move, stop/target on the wrong side of entry,
R not matching the prices when a stop is given, unparseable rows) and lists
problems at the top of the Telegram message so typos get fixed instead of
silently skewing the stats.

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

def fetch_journal_rows() -> tuple:
    """
    Returns (rows, skipped) where rows are parsed trades and skipped is a
    list of human-readable strings for rows that couldn't be used at all
    (so they're reported instead of vanishing silently).
    """
    if not SHEET_CSV_URL:
        raise RuntimeError("JOURNAL_SHEET_CSV_URL is not set.")

    resp = requests.get(SHEET_CSV_URL, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    skipped = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        label = f"{row.get('Open_Date') or '?'} {row.get('Instrument') or '?'}"

        if not row.get("Close_Date"):
            # Completely empty rows are normal in a sheet; a row with data
            # but no Close_Date is worth mentioning.
            if any(row.get(k) for k in ("Instrument", "Open_Date", "Result_R")):
                skipped.append(f"{label}: строка пропущена — не заполнен Close_Date")
            continue
        close_date = _parse_date(row["Close_Date"])
        if close_date is None:
            skipped.append(f"{label}: строка пропущена — не читается Close_Date {row['Close_Date']!r}")
            continue

        open_date = _parse_date(row["Open_Date"]) if row.get("Open_Date") else None

        result_r = _parse_float(row.get("Result_R", ""))
        if result_r is None:
            skipped.append(f"{close_date.strftime('%d.%m.%Y')} {row.get('Instrument') or '?'}: строка пропущена — не читается Result_R {row.get('Result_R', '')!r}")
            continue

        open_price = _parse_float(row.get("Open_Price", ""))
        close_price = _parse_float(row.get("Close_Price", ""))
        stop_price = _parse_float(row.get("Stop_Price", ""))
        target_price = _parse_float(row.get("Target_Price", ""))
        direction_norm = normalize_direction(row.get("Direction", ""))
        open_time = _parse_time(row.get("Open_Time", ""))
        close_time = _parse_time(row.get("Close_Time", ""))

        holding_days = (close_date - open_date).days if open_date else None

        trade = {
            "open_date": open_date,
            "close_date": close_date,
            "open_time": open_time,
            "close_time": close_time,
            "instrument": row.get("Instrument", "").strip() or "?",
            "direction": row.get("Direction", "").strip(),
            "direction_norm": direction_norm,
            "open_price": open_price,
            "close_price": close_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "exit_reason": normalize_exit_reason(row.get("Exit_Reason", "")),
            "result_r": result_r,
            "rule_based": row.get("Rule_Based", "").strip().lower(),
            "cftc_aligned": row.get("CFTC_Aligned", "").strip().lower(),
            "state": normalize_state(row.get("State", "")),
            "setup": row.get("Setup", "").strip() or "(без тега)",
            "holding_days": holding_days,
        }
        trade["planned_rr"] = planned_rr(trade)
        trade["issues"] = validate_trade(trade)
        rows.append(trade)
    return rows, skipped


def normalize_direction(text: str) -> str:
    """Returns "long", "short" or "" (unknown/empty)."""
    t = (text or "").strip().lower()
    if t.startswith(("long", "лонг", "buy", "покуп")):
        return "long"
    if t.startswith(("short", "шорт", "sell", "прод")):
        return "short"
    return ""


def normalize_exit_reason(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return ""
    if t.startswith(("стоп", "stop", "sl")):
        return "Стоп"
    if t.startswith(("тейк", "цел", "tp", "take", "target")):
        return "Тейк"
    if t.startswith(("вруч", "дискр", "manual")):
        return "Вручную"
    if t.startswith(("врем", "time")):
        return "По времени"
    return t.capitalize()


def _signed_move(t: dict):
    """Price move in the trade's favour (+) or against (-), or None."""
    if t["direction_norm"] and t["open_price"] is not None and t["close_price"] is not None:
        raw = t["close_price"] - t["open_price"]
        return raw if t["direction_norm"] == "long" else -raw
    return None


def planned_rr(t: dict):
    """Planned reward:risk from stop/target/entry, or None if not computable."""
    o, st, tg, d = t["open_price"], t["stop_price"], t["target_price"], t["direction_norm"]
    if None in (o, st, tg) or not d:
        return None
    risk = abs(o - st)
    if risk == 0:
        return None
    # Both must be on the correct sides of entry, otherwise it's a typo
    # (reported by validate_trade) and a planned R:R would be meaningless.
    if d == "long" and not (st < o < tg):
        return None
    if d == "short" and not (tg < o < st):
        return None
    return abs(tg - o) / risk


def validate_trade(t: dict) -> list:
    """Returns a list of short Russian problem descriptions (empty = fine)."""
    problems = []

    if t["open_date"] is not None:
        if t["close_date"] < t["open_date"]:
            problems.append("дата закрытия раньше даты открытия (опечатка в годе?)")
        elif (t["close_date"] - t["open_date"]).days > 365:
            problems.append("сделка длится больше года (опечатка в дате?)")
        elif (
            t["close_date"] == t["open_date"]
            and t["open_time"] is not None
            and t["close_time"] is not None
            and t["close_time"] < t["open_time"]
        ):
            problems.append("время закрытия раньше времени открытия")

    move = _signed_move(t)
    if move is not None and move != 0 and abs(t["result_r"]) >= 0.05:
        if (move > 0) != (t["result_r"] > 0):
            problems.append("знак Result_R не совпадает с движением цены (направление/цены/R перепутаны?)")

    o, st, tg, d = t["open_price"], t["stop_price"], t["target_price"], t["direction_norm"]
    if o is not None and d:
        if st is not None and ((d == "long" and st >= o) or (d == "short" and st <= o)):
            problems.append("стоп стоит не с той стороны от цены входа")
        if tg is not None and ((d == "long" and tg <= o) or (d == "short" and tg >= o)):
            problems.append("цель стоит не с той стороны от цены входа")

    if move is not None and st is not None and o is not None and abs(o - st) > 0:
        correct_side = (d == "long" and st < o) or (d == "short" and st > o)
        if correct_side:
            implied_r = move / abs(o - st)
            if abs(implied_r - t["result_r"]) > 0.3:
                problems.append(f"R по ценам ≈ {implied_r:+.2f}, а в таблице {t['result_r']:+.2f}")

    return problems


def normalize_state(text: str) -> str:
    """
    Maps free-typed state text to one of three canonical labels, tolerating
    case and common endings (Усталость / устал / усталый -> Усталость).
    Returns "" if empty. Unrecognized non-empty text is kept as typed
    (capitalized), so a custom state you invent still gets its own bucket.
    """
    t = (text or "").strip().lower()
    if not t:
        return ""
    if t.startswith("устал"):
        return "Усталость"
    if t.startswith("спок"):
        return "Спокойствие"
    if t.startswith("азарт"):
        return "Азарт"
    return t.capitalize()


def _parse_date(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H.%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_float(text: str):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
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


def holding_bucket(days: int) -> str:
    if days <= 0:
        return "Внутри дня"
    if days <= 5:
        return "Свинг (1-5 дн.)"
    return "Позиционная (6+ дн.)"


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


MAX_ISSUES_SHOWN = 10


def build_message(trades: list, period_start: date, period_end: date, issues: list = None) -> str:
    period_label = f"{period_start.strftime('%d.%m.%Y')} — {period_end.strftime('%d.%m.%Y')}"
    lines = [f"\U0001F4D2 Статистика дневника трейдера за период {period_label}\n"]

    if issues:
        lines.append("\u26A0\uFE0F Проверьте данные в таблице:")
        for issue in issues[:MAX_ISSUES_SHOWN]:
            lines.append(f"  • {issue}")
        if len(issues) > MAX_ISSUES_SHOWN:
            lines.append(f"  …и ещё {len(issues) - MAX_ISSUES_SHOWN}")
        lines.append("")

    if not trades:
        lines.append("За этот период закрытых сделок в таблице не найдено.")
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

    lines.append("\U0001F4C5 По дням недели (закрытия сделки):")
    by_weekday = compute_breakdown(trades, lambda t: RUSSIAN_WEEKDAYS[t["close_date"].weekday()])
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

    # Negative durations come from a date typo (already flagged at the top);
    # leave those rows out here rather than miscounting them as intraday.
    duration_trades = [t for t in trades if t["holding_days"] is not None and t["holding_days"] >= 0]
    if duration_trades:
        lines.append("\u23F1 По длительности сделки:")
        by_duration = compute_breakdown(duration_trades, lambda t: holding_bucket(t["holding_days"]))
        for label in ("Внутри дня", "Свинг (1-5 дн.)", "Позиционная (6+ дн.)"):
            if label in by_duration:
                lines.append(fmt_bucket(label, by_duration[label]))
        lines.append("")

    exit_trades = [t for t in trades if t["exit_reason"]]
    if exit_trades:
        lines.append("\U0001F6AA По причине выхода:")
        by_exit = compute_breakdown(exit_trades, lambda t: t["exit_reason"])
        ordered = [k for k in ("Стоп", "Тейк", "Вручную", "По времени") if k in by_exit]
        ordered += sorted(k for k in by_exit if k not in ordered)
        for label in ordered:
            lines.append(fmt_bucket(label, by_exit[label]))
        lines.append("")

    rr_trades = [t for t in trades if t["planned_rr"] is not None]
    if rr_trades:
        avg_rr = sum(t["planned_rr"] for t in rr_trades) / len(rr_trades)
        winners = [t for t in rr_trades if t["result_r"] > 0]
        lines.append("\U0001F4D0 План vs факт (сделки с заданными стопом и целью):")
        if len(rr_trades) < MIN_SAMPLES_FOR_BREAKDOWN:
            lines.append(f"  недостаточно данных ({len(rr_trades)} сделок)")
        else:
            lines.append(f"  Средний плановый R:R: 1:{avg_rr:.1f} ({len(rr_trades)} сделок)")
            if winners:
                avg_win = sum(t["result_r"] for t in winners) / len(winners)
                lines.append(f"  Средний фактический результат выигрышей: {avg_win:+.2f}R")
            losers = [t for t in rr_trades if t["result_r"] < 0]
            if losers:
                avg_loss = sum(t["result_r"] for t in losers) / len(losers)
                lines.append(f"  Средний фактический результат проигрышей: {avg_loss:+.2f}R")
        lines.append("")

    state_trades = [t for t in trades if t["state"]]
    if state_trades:
        lines.append("\U0001F9E0 По состоянию при входе:")
        by_state = compute_breakdown(state_trades, lambda t: t["state"])
        ordered = [k for k in ("Спокойствие", "Усталость", "Азарт") if k in by_state]
        ordered += sorted(k for k in by_state if k not in ordered)
        for label in ordered:
            lines.append(fmt_bucket(label, by_state[label]))
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
    max_len = 4096  # Telegram rejects longer messages outright
    if len(text) > max_len:
        text = text[: max_len - 20].rstrip() + "\n…(обрезано)"
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

    all_rows, skipped = fetch_journal_rows()
    trades = [r for r in all_rows if period_start <= r["close_date"] <= period_end]

    # Flag problems in the reported period and anything closed since
    # (so a typo in a trade that closed this month gets caught before
    # next month's report, not after).
    issues = list(skipped)
    for r in all_rows:
        if r["close_date"] >= period_start:
            label = f"{r['close_date'].strftime('%d.%m.%Y')} {r['instrument']}"
            for problem in r["issues"]:
                issues.append(f"{label}: {problem}")

    print(
        f"Diagnostic: {len(all_rows)} total rows in sheet, {len(trades)} trades closed in "
        f"{period_start}..{period_end}, {len(issues)} data issue(s) flagged."
    )

    message = build_message(trades, period_start, period_end, issues)
    send_telegram_message(message)
    print("Journal stats report sent.")


if __name__ == "__main__":
    main()
