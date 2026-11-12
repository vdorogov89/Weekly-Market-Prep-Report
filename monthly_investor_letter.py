"""
Monthly "letter to the shareholder" (a letter to yourself) -> Telegram.

Runs on the last day of the month (the workflow fires on days 28-31 and this
script exits unless tomorrow is the 1st). It builds a DRAFT of the five-section
letter from the trading journal:
  1. result in R and dollars   2. drawdown   3. what worked
  4. which rules were broken   5. decisions for next month (to be written by you)
The full letter (with account balance and the drawdown in % from the finance
panel) is finished by hand in the Claude Doc; this draft only does the counting.

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JOURNAL_SHEET_CSV_URL
Manual run: set MONTH=YYYY-MM (any day) to build the letter for that month.
"""

import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import trading_journal_digest as jd

LATE_STOP_WORDS = ("после входа", "позже", "внесён", "внесен", "не при входе", "перетянут")
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
             "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def num(v, digits=2, sign=True):
    text = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    return text.replace(",", " ").replace(".", ",").replace("-", "−")


def month_bounds():
    forced = os.environ.get("MONTH", "").strip()
    if forced:
        y, m = (int(x) for x in forced.split("-"))
        start = date(y, m, 1)
    else:
        today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()  # Moscow date
        if (today + timedelta(days=1)).day != 1:
            return None
        start = today.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def drawdown(trades):
    """Max drop of cumulative closed P&L from its running peak (start of month = 0)."""
    peak = cum_r = cum_usd = 0.0
    peak_r = peak_usd = 0.0
    dd_r = dd_usd = 0.0
    for t in sorted(trades, key=lambda x: (x["close_date"], x.get("close_time") or 0)):
        cum_r += t["result_r"]
        cum_usd += (t["result_usd"] or 0.0) + (t["swap"] or 0.0)
        peak_r, peak_usd = max(peak_r, cum_r), max(peak_usd, cum_usd)
        dd_r, dd_usd = max(dd_r, peak_r - cum_r), max(dd_usd, peak_usd - cum_usd)
    return dd_r, dd_usd


def build_letter(trades, start, end):
    name = f"{MONTHS_RU[start.month - 1]} {start.year}"
    lines = [f"✉️ Письмо акционеру — {name} (черновик)", ""]
    if not trades:
        return "\n".join(lines + ["Закрытых сделок за месяц нет. Разделы 1–4 пустые, заполни только решения."])

    total_r = sum(t["result_r"] for t in trades)
    usd = sum(t["result_usd"] or 0.0 for t in trades)
    swap = sum(t["swap"] or 0.0 for t in trades)
    wins = [t for t in trades if t["result_r"] > 0]
    lines += ["1. ИТОГ",
              f"Сделок: {len(trades)}, прибыльных: {len(wins)} ({100 * len(wins) // len(trades)}%).",
              f"Результат: {num(total_r)}R, {num(usd, 0)} $ без свопа, своп {num(swap, 0)} $ (с ним {num(usd + swap, 0)} $)."]
    for t in sorted(trades, key=lambda x: x["close_date"]):
        lines.append(f"  {t['close_date'].strftime('%d.%m')} {t['instrument']} {t['direction_norm'] or ''}: "
                     f"{num(t['result_r'])}R, {num(t['result_usd'] or 0, 0)} $")
    lines += ["Счёт на конец месяца: ____ $ (из панели).", ""]

    dd_r, dd_usd = drawdown(trades)
    lines += ["2. ПРОСАДКА",
              f"По закрытым сделкам от пика внутри месяца: {num(dd_r, 2, False)}R, {num(dd_usd, 0, False)} $.",
              "Макс. просадка счёта, %: ____ (из панели, учитывает открытые позиции).", ""]

    lines.append("3. ЧТО СРАБОТАЛО")
    best = max(trades, key=lambda t: t["result_r"])
    lines.append(f"Лучшая: {best['instrument']} {num(best['result_r'])}R ({best['setup']}).")
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t["setup"]].append(t["result_r"])
    for setup, rs in sorted(by_setup.items(), key=lambda kv: -sum(kv[1])):
        lines.append(f"  {setup}: {len(rs)} сд., {num(sum(rs))}R")
    lines.append("")

    lines.append("4. НАРУШЕННЫЕ ПРАВИЛА (по журналу)")
    broke = False
    no_rule = [t for t in trades if t["rule_based"].startswith("нет")]
    if no_rule:
        broke = True
        lines.append(f"Вход без правила (Rule_Based Нет): {len(no_rule)} сд., "
                     f"{num(sum(t['result_r'] for t in no_rule))}R, "
                     f"{num(sum(t['result_usd'] or 0 for t in no_rule), 0)} $.")
    emo = [t for t in trades if t["state"] in ("азарт", "усталость")]
    if emo:
        broke = True
        lines.append(f"Не в состоянии «спокойствие»: {len(emo)} сд. "
                     f"({', '.join(t['instrument'] for t in emo)}), {num(sum(t['result_r'] for t in emo))}R.")
    late = [t for t in trades if any(w in (t.get("notes") or "").lower() for w in LATE_STOP_WORDS)]
    if late:
        broke = True
        lines.append("Стоп/цель/параметры внесены или изменены позже входа (по заметкам): "
                     + ", ".join(f"{t['instrument']} {t['close_date'].strftime('%d.%m')}" for t in late) + ".")
    if not broke:
        lines.append("Автопроверка нарушений не нашла. Проверь вручную: размер риска, кучность входов.")
    lines.append("Размер риска и кучность входов бот не видит — допиши сам.")
    lines.append("")

    lines += ["5. РЕШЕНИЯ НА СЛЕДУЮЩИЙ МЕСЯЦ", "1) ____", "2) ____", "3) ____", "",
              "Перед тем как писать решения, перечитай раздел 4 двух предыдущих писем."]
    return "\n".join(lines)


def main():
    bounds = month_bounds()
    if bounds is None:
        print("Not the last day of the month — nothing to do.")
        return
    start, end = bounds
    rows, _ = jd.fetch_journal_rows()
    trades = [r for r in rows if start <= r["close_date"] <= end]
    jd.send_telegram_message(build_letter(trades, start, end))
    print(f"Letter draft sent for {start:%Y-%m}: {len(trades)} trades.")


if __name__ == "__main__":
    sys.exit(main())
