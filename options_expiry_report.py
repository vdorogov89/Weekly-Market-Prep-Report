"""
Monthly / quarterly options expiration report -> Telegram.

Large US expirations (the third Friday of every month; March, June, September
and December are the quarterly "triple witching" ones) burn a big share of the
open interest and gamma at once. This script shows, for the instruments you
currently hold plus a fixed watchlist (NDX, SPX), how much open interest and
gamma expires, where the big call/put strikes ("walls") are relative to spot,
and the max-pain level.

It sends two messages per expiration: on the Monday of expiration week and on
the expiration day itself (both before the US open).

Data: Cboe delayed quotes (free JSON, open interest + gamma per option,
refreshed once a day). Open interest does not say who is long or short, so the
script shows SIZES and LEVELS only and makes no claim about dealer direction.

Instruments: open positions are read from the trading journal (rows without a
Close_Date); the mapping to option underlyings is in UNDERLYING_MAP. Futures,
FX and oil have no equivalent free data and are listed as "no data".

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JOURNAL_SHEET_CSV_URL
Optional: FORCE_EXPIRY=YYYYMMDD (build the report for that date now, ignore the
schedule and the state), DRY_RUN=1 (print instead of sending).
"""

import calendar
import collections
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

import trading_journal_digest as jd

STATE_FILE = "options_expiry_state.json"
WATCHLIST = ["NDX", "SPX"]
UNDERLYING_MAP = {          # journal instrument -> Cboe symbol
    "NDX": "_NDX", "#NDX": "_NDX", "SPX": "_SPX", "US500": "_SPX",
    "AVGO": "AVGO", "#AVGO": "AVGO", "AMZN": "AMZN", "#AMZN": "AMZN",
    "GOOGL": "GOOGL", "#GOOGL": "GOOGL", "MSTR": "MSTR", "#MSTR": "MSTR",
    "XAUUSD": "GLD", "XAUUSDRFD": "GLD",
}
NAMES = {"_NDX": "NDX", "_SPX": "SPX", "GLD": "GLD (золото)"}
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
WALL_RANGE = 0.10           # walls are searched within ±10% of spot
HIGH_GAMMA_SHARE = 25.0     # % of all gamma expiring at once is "a lot"
NEAR_WALL = 1.0             # % distance to a wall counted as "at the strike"
MIN_WALL_SHARE = 0.08       # a wall must hold >= 8% of the expiring calls (puts) to be flagged


# ---------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------

def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def is_market_holiday(d: date) -> bool:
    """Only holidays that can fall on a third Friday: Good Friday, Juneteenth."""
    if d == _easter(d.year) - timedelta(days=2):
        return True
    june19 = date(d.year, 6, 19)
    observed = june19 - timedelta(days=1) if june19.weekday() == 5 else \
        june19 + timedelta(days=1) if june19.weekday() == 6 else june19
    return d == observed


def monthly_expiry(year: int, month: int) -> date:
    fridays = [x for x in calendar.Calendar().itermonthdates(year, month)
               if x.month == month and x.weekday() == 4]
    d = fridays[2]
    while is_market_holiday(d):
        d -= timedelta(days=1)
    return d


def next_expiry(today: date) -> date:
    y, m = today.year, today.month
    for _ in range(3):
        d = monthly_expiry(y, m)
        if d >= today:
            return d
        m += 1
        if m == 13:
            y, m = y + 1, 1
    raise RuntimeError("no expiry found")


def is_quarterly(d: date) -> bool:
    return d.month in (3, 6, 9, 12)


# ---------------------------------------------------------------------
# Option data
# ---------------------------------------------------------------------

def fetch_chain(symbol: str):
    url = f"https://cdn-api.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
    r.raise_for_status()
    j = r.json()
    return j["data"].get("current_price") or j["data"].get("close"), j["data"]["options"], j.get("timestamp", "")


def analyse_symbol(symbol: str, expiry: date):
    spot, options, asof = fetch_chain(symbol)
    key = expiry.strftime("%y%m%d")
    total_oi = total_gamma = 0.0
    exp = {"c": 0.0, "p": 0.0, "gamma": 0.0, "by": collections.defaultdict(lambda: [0.0, 0.0])}
    for o in options:
        m = OCC.match(o["option"])
        if not m:
            continue
        _, dt, cp, strike = m.groups()
        oi = o.get("open_interest") or 0.0
        gamma_dollars = (o.get("gamma") or 0.0) * oi * 100 * spot * spot * 0.01
        total_oi += oi
        total_gamma += gamma_dollars
        if dt != key:
            continue
        k = int(strike) / 1000
        if cp == "C":
            exp["c"] += oi
            exp["by"][k][0] += oi
        else:
            exp["p"] += oi
            exp["by"][k][1] += oi
        exp["gamma"] += gamma_dollars
    n = exp["c"] + exp["p"]
    if not n or not total_oi:
        return None
    by = exp["by"]
    near = {k: v for k, v in by.items() if abs(k / spot - 1) <= WALL_RANGE}
    call_wall = max(near, key=lambda k: near[k][0]) if near else None
    put_wall = max(near, key=lambda k: near[k][1]) if near else None
    strikes = sorted(by)
    pain = min(strikes, key=lambda s: sum(by[k][0] * max(s - k, 0) + by[k][1] * max(k - s, 0) for k in strikes))
    return {
        "spot": spot, "asof": asof, "oi": n, "oi_share": n / total_oi * 100,
        "gamma_share": exp["gamma"] / total_gamma * 100 if total_gamma else 0.0,
        "pc": exp["p"] / max(exp["c"], 1.0),
        "call_wall": call_wall, "put_wall": put_wall, "pain": pain,
        "call_wall_share": near[call_wall][0] / max(exp["c"], 1.0) if call_wall else 0.0,
        "put_wall_share": near[put_wall][1] / max(exp["p"], 1.0) if put_wall else 0.0,
    }


def fmt_level(x, spot):
    if x is None:
        return "—"
    return f"{x:,.0f}".replace(",", " ") if spot > 1000 else f"{x:g}"


def symbol_block(symbol: str, a: dict) -> list:
    name = NAMES.get(symbol, symbol)
    spot = a["spot"]
    d = lambda lvl: f"{(lvl / spot - 1) * 100:+.1f}%" if lvl else ""
    lines = [f"{name} (спот {fmt_level(spot, spot)})",
             f"  Сгорает {a['oi_share']:.0f}% открытого интереса и {a['gamma_share']:.0f}% гаммы; "
             f"P/C {a['pc']:.2f}",
             f"  Колл-стена {fmt_level(a['call_wall'], spot)} ({d(a['call_wall'])}), "
             f"пут-стена {fmt_level(a['put_wall'], spot)} ({d(a['put_wall'])}), "
             f"max pain {fmt_level(a['pain'], spot)} ({d(a['pain'])})"]
    notes = []
    if a["gamma_share"] >= HIGH_GAMMA_SHARE:
        notes.append("много гаммы сгорает разом")
    for lvl, share, label in ((a["call_wall"], a["call_wall_share"], "колл-стены"),
                              (a["put_wall"], a["put_wall_share"], "пут-стены")):
        if lvl and share >= MIN_WALL_SHARE and abs(lvl / spot - 1) * 100 <= NEAR_WALL:
            notes.append(f"цена у {label}")
    if notes:
        lines.append("  ⚠ " + "; ".join(notes))
    return lines


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------

def open_positions_symbols():
    """(symbols with option data, instruments without) for currently open journal rows."""
    jd.fetch_journal_rows()
    syms, missing = [], []
    for p in jd.OPEN_POSITIONS:
        inst = p["instrument"].strip().upper()
        sym = UNDERLYING_MAP.get(inst)
        if sym:
            if sym not in syms:
                syms.append(sym)
        elif inst not in missing:
            missing.append(inst)
    return syms, missing


def build_report(expiry: date, today: date) -> str:
    kind = "КВАРТАЛЬНАЯ" if is_quarterly(expiry) else "месячная"
    days = (expiry - today).days
    when = "сегодня" if days == 0 else f"через {days} дн."
    syms, missing = open_positions_symbols()
    for w in WATCHLIST:
        sym = UNDERLYING_MAP[w]
        if sym not in syms:
            syms.append(sym)
    lines = [f"📅 Экспирация опционов {expiry:%d.%m.%Y} ({kind}), {when}", ""]
    asof = ""
    any_data = False
    for sym in syms:
        try:
            a = analyse_symbol(sym, expiry)
        except Exception as exc:  # noqa: BLE001
            lines += [f"{NAMES.get(sym, sym)}: данные недоступны ({str(exc)[:60]})", ""]
            continue
        if a is None:
            lines += [f"{NAMES.get(sym, sym)}: на эту дату опционов нет", ""]
            continue
        any_data = True
        asof = a["asof"] or asof
        lines += symbol_block(sym, a) + [""]
    if not any_data:
        raise RuntimeError("no option data for any symbol")
    if missing:
        lines += ["Нет бесплатных данных по опционам: " + ", ".join(missing), ""]
    lines += [f"Данные Cboe на {asof or '?'} (задержка, обновление раз в сутки).",
              "Акции и ETF экспирируются по закрытию 16:00 по Нью-Йорку; месячные NDX/SPX — по утренней расчётной цене пятницы.",
              "Открытый интерес не показывает, кто в какой позиции: это размеры и уровни, а не прогноз направления.",
              "Не инвестиционная рекомендация."]
    return "\n".join(lines)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def main() -> None:
    dry = bool(os.environ.get("DRY_RUN"))
    forced = os.environ.get("FORCE_EXPIRY", "").strip()
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()  # Moscow date
    state = load_state()
    if forced:
        expiry = date(int(forced[:4]), int(forced[4:6]), int(forced[6:8]))
        tag = None
    else:
        if today.weekday() >= 5:
            print("Weekend - nothing to do.")
            return
        expiry = next_expiry(today)
        monday = expiry - timedelta(days=expiry.weekday())
        if today == expiry:
            tag = f"{expiry.isoformat()}:day"
        elif today == monday or (today > monday and today < expiry and f"{expiry.isoformat()}:week" not in state):
            tag = f"{expiry.isoformat()}:week"   # first run of expiration week (also catches a missed Monday)
        else:
            print(f"Next expiry {expiry}: nothing due today.")
            return
        if tag in state:
            print(f"{tag} already sent.")
            return
    text = build_report(expiry, today)
    if dry:
        print(text)
        return
    jd.send_telegram_message(text)
    if tag:
        state[tag] = datetime.now(timezone.utc).isoformat()
        state = {k: v for k, v in state.items() if k >= (today - timedelta(days=90)).isoformat()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    print(f"Sent expiration report for {expiry}.")


if __name__ == "__main__":
    sys.exit(main())
