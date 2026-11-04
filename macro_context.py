"""
Daily macro context -> Telegram: where the wind blows for USD, EUR, GBP, CHF,
JPY and gold.

This is a description of the CURRENT background, not a forecast. A back-test
on 2012-2026 weekly data showed that rate differentials explain part of what
already happened (weekly correlation of the US-EA 2-year spread with EURUSD is
about -0.3, i.e. roughly 10% of the variance) but have almost no predictive
power for the next week (|correlation| about 0.1). So the report is built to
answer "what is moving the market right now and does the usual link work?",
not "where will the price go".

Data (all free, no scraping):
  - FRED (fredgraph.csv, no key): US 2Y/10Y yields, 10Y real yield (TIPS),
    VIX, S&P 500. Values are the previous business day's close.
  - ECB data portal (no key): euro-area AAA yield curve, 2Y and 10Y spot
    rates (a close proxy for Bund yields), previous business day.
  - Twelve Data (secret TWELVE_DATA_API_KEY): EURUSD, GBPUSD, USDJPY, USDCHF,
    XAUUSD daily bars including today's forming bar. If the key is missing
    or the call fails, FRED's FX rates are used instead (they lag a few days;
    gold is then skipped).
  - Forex Factory weekly feed: today's High (and USD/EUR Medium) events.
  - CFTC Commitments of Traders (Socrata API): weekly positioning, shown as a
    reference line per instrument; it is NOT part of the wind score.
  - Optional: the trading journal (JOURNAL_SHEET_CSV_URL) to show whether the
    wind is with or against your open positions.

How the "wind" of an instrument is built (see README):
  rate signal  = sign of the driver's 5-day change, only if it is big enough
                 (|z| >= 0.7 versus its own last-year 5-day changes), turned
                 into the instrument's direction by the usual sign of the link
  link quality = 60-day correlation of daily changes, driver vs instrument:
                 works (>= 0.25 in the usual direction, weight 1),
                 weak (weight 0.5), broken (opposite direction, weight 0)
  trend        = +1 close > SMA20 > SMA50, -1 close < SMA20 < SMA50, else 0
  score        = rate signal * weight + trend
Drivers: EURUSD - US minus euro-area 2Y spread; GBPUSD, USDJPY, USDCHF - US 2Y
(no free daily UK/JP/CH yield series exists); XAUUSD - US 10Y real yield.

Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required (unless DRY_RUN)
  TWELVE_DATA_API_KEY                    recommended
  JOURNAL_SHEET_CSV_URL                  optional (open positions block)
  STATE_FILE                             default macro_context_state.json
  DRY_RUN=1                              print instead of sending, state not saved
"""

import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = os.environ.get("STATE_FILE", "macro_context_state.json")
TD_KEY = os.environ.get("TWELVE_DATA_API_KEY")
JOURNAL_URL = os.environ.get("JOURNAL_SHEET_CSV_URL")

Z_MIN = 0.7          # driver move needed to count as a signal
LINK_OK = 0.25       # |corr| needed for "link works"
CORR_WINDOW = 60
ZSTAT_WINDOW = 250
WD_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ---------------------------------------------------------------------
# Series helpers: a series is a list of (date, value) sorted by date
# ---------------------------------------------------------------------

def _get(url, params=None, timeout=40, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "macro-context-bot"})
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def fetch_fred(series_id, days=420):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    r = _get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": series_id, "cosd": start})
    out = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 2 or not row[0][:1].isdigit():
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue  # FRED uses "." for missing days
    if not out:
        raise RuntimeError(f"FRED {series_id}: empty")
    return out


def fetch_ecb(tenor, days=420):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.{tenor}"
    r = _get(url, {"startPeriod": start, "format": "csvdata"})
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            out.append((row["TIME_PERIOD"], float(row["OBS_VALUE"])))
        except (KeyError, ValueError):
            continue
    out.sort()
    if not out:
        raise RuntimeError(f"ECB {tenor}: empty")
    return out


TD_SYMBOLS = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
              "USDCHF": "USD/CHF", "XAUUSD": "XAU/USD"}
FRED_FX = {"EURUSD": "DEXUSEU", "GBPUSD": "DEXUSUK", "USDJPY": "DEXJPUS", "USDCHF": "DEXSZUS"}


def fetch_twelvedata():
    if not TD_KEY:
        raise RuntimeError("no TWELVE_DATA_API_KEY")
    r = _get("https://api.twelvedata.com/time_series", {
        "symbol": ",".join(TD_SYMBOLS.values()), "interval": "1day",
        "outputsize": 300, "apikey": TD_KEY})
    data = r.json()
    out = {}
    for name, sym in TD_SYMBOLS.items():
        block = data.get(sym) if sym in data else None
        if block is None and len(TD_SYMBOLS) == 1:
            block = data
        if not block or block.get("status") == "error" or "values" not in block:
            continue
        pts = []
        for v in block["values"]:
            try:
                pts.append((v["datetime"][:10], float(v["close"])))
            except (KeyError, ValueError):
                continue
        pts.sort()
        if len(pts) > 60:
            out[name] = pts
    if not out:
        raise RuntimeError("Twelve Data returned no usable series")
    return out


def fetch_fx():
    """Returns (dict name->series, note)."""
    try:
        return fetch_twelvedata(), ""
    except Exception as e:  # noqa: BLE001 - any failure falls back to FRED
        print(f"Twelve Data failed ({e}); falling back to FRED FX")
    out = {name: fetch_fred(sid) for name, sid in FRED_FX.items()}
    return out, "FX из FRED (резерв, с задержкой), золото недоступно"


def vals(s):
    return [v for _, v in s]


def align(a, b):
    db = dict(b)
    return [(d, v, db[d]) for d, v in a if d in db]


def spread(a, b):
    return [(d, x - y) for d, x, y in align(a, b)]


def change(s, n):
    v = vals(s)
    return v[-1] - v[-1 - n] if len(v) > n else None


def logret(s, n):
    v = vals(s)
    return math.log(v[-1] / v[-1 - n]) if len(v) > n else None


def zscore(changes_now, history_changes):
    if changes_now is None or len(history_changes) < 30:
        return None
    mean = sum(history_changes) / len(history_changes)
    var = sum((x - mean) ** 2 for x in history_changes) / (len(history_changes) - 1)
    sd = math.sqrt(var)
    return changes_now / sd if sd > 0 else None


def z_of_diff(s, n=5, log=False):
    v = vals(s)[-(ZSTAT_WINDOW + n):]
    if len(v) < n + 30:
        return None
    if log:
        hist = [math.log(v[i] / v[i - n]) for i in range(n, len(v))]
    else:
        hist = [v[i] - v[i - n] for i in range(n, len(v))]
    return zscore(hist[-1], hist[:-1] if len(hist) > 31 else hist)


def sma(s, n):
    v = vals(s)
    return sum(v[-n:]) / n if len(v) >= n else None


def corr_daily(price, driver, log_price=True):
    """Correlation of daily changes over the last CORR_WINDOW common days."""
    common = align(price, driver)[-(CORR_WINDOW + 1):]
    if len(common) < 40:
        return None
    xs, ys = [], []
    for i in range(1, len(common)):
        p0, p1 = common[i - 1][1], common[i][1]
        xs.append(math.log(p1 / p0) if log_price else p1 - p0)
        ys.append(common[i][2] - common[i - 1][2])
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def sign(x, dead=0.0):
    if x is None or abs(x) <= dead:
        return 0
    return 1 if x > 0 else -1


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------

def num(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def snum(x, d=2):
    t = f"{x:+.{d}f}"
    if float(t) == 0:
        t = t[1:]  # no "+0,0" / "-0,0"
    return t.replace(".", ",").replace("-", "−")


def bp(x):
    return "н/д" if x is None else snum(x * 100, 0)


def pct(x, d=1):
    return "н/д" if x is None else snum(x * 100, d) + "%"


def dshort(iso):
    return f"{iso[8:10]}.{iso[5:7]}"


# ---------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------

# name: (driver key, usual sign of corr(instrument return, driver change), driver label)
INSTRUMENTS = {
    "EURUSD": ("sp2", -1, "спред US−EA 2Y"),
    "GBPUSD": ("us2", -1, "US 2Y"),
    "USDJPY": ("us2", +1, "US 2Y"),
    "USDCHF": ("us2", +1, "US 2Y"),
    "XAUUSD": ("rr10", -1, "реальная 10Y"),
}
# for the report: 'up' means the instrument price rises
DISPLAY_ORDER = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "XAUUSD"]
DECIMALS = {"EURUSD": 4, "GBPUSD": 4, "USDJPY": 2, "USDCHF": 4, "XAUUSD": 0}


def analyse_instrument(name, price, drivers):
    key, usual, dlabel = INSTRUMENTS[name]
    drv = drivers.get(key)
    res = {"name": name, "last": vals(price)[-1], "date": price[-1][0],
           "ret1": logret(price, 1), "ret5": logret(price, 5), "driver_label": dlabel,
           "rate": 0, "link": None, "link_state": "нет данных", "weight": 0.5,
           "trend": 0, "z_price": z_of_diff(price, 5, log=True), "dz": None, "d5": None}
    s20, s50 = sma(price, 20), sma(price, 50)
    last = res["last"]
    if s20 and s50:
        res["trend"] = 1 if last > s20 > s50 else (-1 if last < s20 < s50 else 0)
    if drv:
        res["d5"] = change(drv, 5)
        res["dz"] = z_of_diff(drv, 5)
        if res["dz"] is not None and abs(res["dz"]) >= Z_MIN:
            res["rate"] = usual * sign(res["dz"])
        c = corr_daily(price, drv)
        res["link"] = c
        if c is not None:
            aligned_c = c * usual
            if aligned_c >= LINK_OK:
                res["link_state"], res["weight"] = "связь работает", 1.0
            elif aligned_c <= -LINK_OK:
                res["link_state"], res["weight"] = "связь сломана", 0.0
            else:
                res["link_state"], res["weight"] = "связь слабая", 0.5
    res["score"] = res["rate"] * res["weight"] + res["trend"]
    sc = res["score"]
    res["label"] = ("▲▲ вверх" if sc >= 1.5 else "▲ слабо вверх" if sc >= 0.5
                    else "▼▼ вниз" if sc <= -1.5 else "▼ слабо вниз" if sc <= -0.5
                    else "◆ нет ясности")
    res["diverge"] = bool(res["z_price"] is not None and abs(res["z_price"]) >= 1.0
                          and res["rate"] != 0 and res["weight"] >= 1.0
                          and sign(res["z_price"]) != res["rate"])
    return res


def usd_basket(fx):
    """Equal-weight USD strength vs EUR, GBP, JPY, CHF: log-return over n days."""
    needed = [n for n in ("EURUSD", "GBPUSD", "USDJPY", "USDCHF") if n in fx]
    if len(needed) < 3:
        return None
    out = {}
    for n in (5, 20):
        parts = []
        for name in needed:
            r = logret(fx[name], n)
            if r is None:
                return None
            parts.append(r if name.startswith("USD") else -r)
        out[n] = sum(parts) / len(parts)
    return out


def risk_regime(vix, spx):
    res = {"vix": vals(vix)[-1], "vix_date": vix[-1][0], "vix5": change(vix, 5),
           "spx5": logret(spx, 5), "spx_trend": 0, "spx_date": spx[-1][0]}
    s50 = sma(spx, 50)
    if s50:
        res["spx_trend"] = 1 if vals(spx)[-1] > s50 else -1
    v = res["vix"]
    res["vix_state"] = ("спокойно" if v < 15 else "норма" if v < 20 else
                        "напряжение" if v < 30 else "паника")
    res["wind"] = (1 if res["spx_trend"] > 0 and v < 22 else
                   -1 if res["spx_trend"] < 0 and v >= 22 else 0)
    return res


# ---------------------------------------------------------------------
# Calendar of the day (Forex Factory feed, same source as the other reports)
# ---------------------------------------------------------------------

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL_COUNTRIES = {"USD": "США", "EUR": "Еврозона", "GBP": "Британия", "CHF": "Швейцария", "JPY": "Япония"}
CAL_MEDIUM_FOR = {"USD", "EUR"}   # Medium impact only for these; High for all
CAL_MAX_LINES = 9


def fetch_calendar(now_msk):
    """Events of the Moscow date `now_msk`, as (time_msk, country, title, impact, forecast, previous)."""
    r = _get(CALENDAR_URL, tries=2, timeout=20)
    out = []
    for e in r.json():
        try:
            c, imp = e.get("country"), e.get("impact")
            if c not in CAL_COUNTRIES or imp not in ("High", "Medium"):
                continue
            if imp == "Medium" and c not in CAL_MEDIUM_FOR:
                continue
            when = datetime.fromisoformat(e["date"]).astimezone(timezone(timedelta(hours=3)))
            if when.date() != now_msk.date():
                continue
            out.append((when, c, e.get("title", "?"), imp, e.get("forecast") or "", e.get("previous") or ""))
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def calendar_lines(events):
    lines = []
    for when, c, title, imp, fc, prev in events[:CAL_MAX_LINES]:
        mark = "\U0001F534" if imp == "High" else "\U0001F7E1"
        tail = ""
        if fc or prev:
            tail = f" (прогноз {fc or '—'}, пред. {prev or '—'})"
        lines.append(f"{mark} {when.strftime('%H:%M')} {c} {title}{tail}")
    if len(events) > CAL_MAX_LINES:
        lines.append(f"…и ещё {len(events) - CAL_MAX_LINES}")
    return lines


# ---------------------------------------------------------------------
# CFTC positioning (non-commercial, legacy futures only; updated weekly)
# ---------------------------------------------------------------------

CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
# instrument -> (market name, invert): invert = True when futures-long means the pair falls
CFTC_MARKETS = {
    "EURUSD": ("EURO FX - CHICAGO MERCANTILE EXCHANGE", False),
    "GBPUSD": ("BRITISH POUND - CHICAGO MERCANTILE EXCHANGE", False),
    "USDJPY": ("JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE", True),
    "USDCHF": ("SWISS FRANC - CHICAGO MERCANTILE EXCHANGE", True),
    "XAUUSD": ("GOLD - COMMODITY EXCHANGE INC.", False),
}


def fetch_cftc():
    """-> {instrument: {"date", "level" (+1 pair up / -1 down), "long_pct", "flow" (contracts, in the pair's direction), "flow_side"}}"""
    out = {}
    for name, (market, invert) in CFTC_MARKETS.items():
        try:
            r = _get(CFTC_URL, {"$where": f"market_and_exchange_names='{market}'",
                                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 1},
                     timeout=20, tries=3)
            rows = r.json()
            if not rows:
                continue
            row = rows[0]
            lg, sh = float(row["noncomm_positions_long_all"]), float(row["noncomm_positions_short_all"])
            if lg + sh == 0:
                continue
            long_pct = 100 * lg / (lg + sh)
            flow = float(row["change_in_noncomm_long_all"]) - float(row["change_in_noncomm_short_all"])
            k = -1 if invert else 1
            out[name] = {"date": row["report_date_as_yyyy_mm_dd"][:10], "long_pct": long_pct,
                         "level": k * (1 if long_pct > 50 else -1), "flow": k * flow,
                         "flow_side": k * sign(flow)}
        except Exception as e:  # noqa: BLE001
            print(f"CFTC {name} failed: {e}")
    return out


def cftc_line(c, invert_pair):
    arrow = {1: "▲", -1: "▼", 0: "◆"}
    side = c["long_pct"] if not invert_pair else 100 - c["long_pct"]
    return (f"CFTC на {dshort(c['date'])}: уровень {arrow[c['level']]} (большинство "
            f"{'лонг' if c['level'] > 0 else 'шорт'} по паре, {num(max(side, 100 - side), 0)}%), "
            f"поток {arrow[c['flow_side']]} ({snum(c['flow'] / 1000, 1)}к)")


# ---------------------------------------------------------------------
# Open positions from the journal
# ---------------------------------------------------------------------

STOCK_LIKE = {"NDX", "AVGO", "MSTR", "GOOGL", "AMZN"}


def fetch_open_positions():
    if not JOURNAL_URL:
        return []
    try:
        r = _get(JOURNAL_URL, timeout=30)
        r.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"Journal unavailable: {e}")
        return []
    out = []
    for raw in csv.DictReader(io.StringIO(r.text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("Instrument") and row.get("Open_Date") and not row.get("Close_Date"):
            d = row.get("Direction", "").lower()
            direction = 1 if d.startswith(("long", "лонг", "buy")) else -1 if d.startswith(("short", "шорт", "sell")) else 0
            if direction:
                out.append({"instrument": row["Instrument"], "direction": direction,
                            "dir_text": "Long" if direction > 0 else "Short"})
    return out


def position_line(pos, results, risk):
    ins, d = pos["instrument"], pos["direction"]
    if ins in results:
        r = results[ins]
        wind = sign(r["score"], 0.49)
        why = r["label"]
    elif ins in STOCK_LIKE and risk:
        wind = risk["wind"]
        why = f"фон риска (VIX {num(risk['vix'], 1)}, S&P {'выше' if risk['spx_trend'] > 0 else 'ниже'} SMA50)"
    else:
        return f"• {ins} {pos['dir_text']}: нет свежих данных для оценки"
    if wind == 0:
        verdict = "нейтрально"
    elif wind == d:
        verdict = "по ветру"
    else:
        verdict = "против ветра"
    return f"• {ins} {pos['dir_text']}: {verdict} ({why})"


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------

def build_message(now, data, results, basket, risk, positions, fx_note, prev_labels, events=None, cftc=None):
    lines = [f"\U0001F9ED Макро-контекст на {now.strftime('%d.%m.%Y')} ({WD_RU[now.weekday()]})"]
    us2, sp2, rr10 = data.get("us2"), data.get("sp2"), data.get("rr10")
    stamp = us2[-1][0] if us2 else None
    lines.append(f"Ставки на закрытие {dshort(stamp)}; цены на момент отчёта." if stamp else "")
    lines.append("")

    lines.append("ДОЛЛАР И СТАВКИ")
    if us2:
        lines.append(f"• US 2Y {num(us2[-1][1])}% (1д {bp(change(us2, 1))}, 5д {bp(change(us2, 5))}, 20д {bp(change(us2, 20))} б.п.)")
    if data.get("us10"):
        s = data["us10"]
        lines.append(f"• US 10Y {num(s[-1][1])}% (5д {bp(change(s, 5))} б.п.)")
    if rr10:
        be = data.get("be10")
        extra = f", инфл. ожидания {num(be[-1][1])}%" if be else ""
        lines.append(f"• Реальная 10Y {num(rr10[-1][1])}% (5д {bp(change(rr10, 5))} б.п.{extra})")
    if sp2:
        lines.append(f"• Спред US−EA 2Y {num(sp2[-1][1])} п.п. (5д {bp(change(sp2, 5))}, 20д {bp(change(sp2, 20))} б.п.)")
    if data.get("sp10"):
        s = data["sp10"]
        lines.append(f"• Спред US−EA 10Y {num(s[-1][1])} п.п. (5д {bp(change(s, 5))}, 20д {bp(change(s, 20))} б.п.)")
    if basket:
        lines.append(f"• Доллар к корзине EUR/GBP/JPY/CHF: 5д {pct(basket[5])}, 20д {pct(basket[20])}")
    lines.append("")

    if risk:
        lines.append("РИСК")
        lines.append(f"• VIX {num(risk['vix'], 1)} — {risk['vix_state']} (5д {snum(risk['vix5'], 1) if risk['vix5'] is not None else 'н/д'})")
        lines.append(f"• S&P 500 {'выше' if risk['spx_trend'] > 0 else 'ниже'} SMA50, 5д {pct(risk['spx5'])}")
        lines.append("")

    if events is not None:
        lines.append("СОБЫТИЯ СЕГОДНЯ (МСК)")
        lines.extend(calendar_lines(events) if events else ["Важных событий по USD/EUR/GBP/CHF/JPY нет."])
        lines.append("")

    lines.append("ВАЛЮТЫ И ЗОЛОТО (ветер = ставки + тренд)")
    for name in DISPLAY_ORDER:
        r = results.get(name)
        if not r:
            continue
        link = f"{r['link_state']}" + (f" {snum(r['link'], 2)}" if r["link"] is not None else "")
        drv = ""
        if r["d5"] is not None:
            drv = f"{r['driver_label']} 5д {bp(r['d5'])} б.п."
        trend = {1: "тренд вверх", -1: "тренд вниз", 0: "тренд смешанный"}[r["trend"]]
        stale = f", на {dshort(r['date'])}" if r["date"] < now.strftime("%Y-%m-%d")[:10] and fx_note else ""
        lines.append(f"{r['label'].split()[0]} {name} {num(r['last'], DECIMALS[name])} "
                     f"(1д {pct(r['ret1'])}, 5д {pct(r['ret5'])}{stale}) — {r['label'].split(' ', 1)[1]}")
        lines.append(f"   {drv}; {link}; {trend}")
        if cftc and name in cftc:
            lines.append("   " + cftc_line(cftc[name], CFTC_MARKETS[name][1]))
        if r["diverge"]:
            lines.append("   ⚠ цена идёт против ставок при работающей связи: движение не от ставок")
    if cftc:
        lines.append("(CFTC: уровень = у кого большинство позиций, поток = куда крупные спекулянты "
                     "добавили за неделю; ▲/▼ и цифры даны по направлению пары; в оценку ветра не входит)")
    if fx_note:
        lines.append(f"({fx_note})")
    lines.append("")

    if positions:
        lines.append("МОИ ПОЗИЦИИ")
        for pos in positions:
            lines.append(position_line(pos, results, risk))
        lines.append("")

    changes = []
    for name in DISPLAY_ORDER:
        r = results.get(name)
        if r and name in prev_labels and prev_labels[name] != r["label"]:
            changes.append(f"{name}: {prev_labels[name]} → {r['label']}")
    if changes:
        lines.append("ИЗМЕНИЛОСЬ С ПРОШЛОГО ОТЧЁТА")
        lines.extend(f"• {c}" for c in changes)
        lines.append("")

    lines.append("Это описание фона, а не прогноз: спреды и ставки объясняют лишь часть "
                 "движения (около 10% недельной дисперсии EURUSD) и почти не предсказывают "
                 "следующую неделю. Решения по сделкам принимаете вы.")
    return "\n".join(x for x in lines if x is not None)


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
    if len(text) > 4096:
        text = text[:4076].rstrip() + "\n…(обрезано)"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("labels", {}) if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def main():
    now = datetime.now(timezone.utc) + timedelta(hours=3)  # Moscow date for the header
    data = {}
    for key, sid in (("us2", "DGS2"), ("us10", "DGS10"), ("rr10", "DFII10"),
                     ("be10", "T10YIE"), ("vix", "VIXCLS"), ("spx", "SP500")):
        try:
            data[key] = fetch_fred(sid)
        except Exception as e:  # noqa: BLE001
            print(f"{sid} failed: {e}")
    for key, tenor in (("ea2", "SR_2Y"), ("ea10", "SR_10Y")):
        try:
            data[key] = fetch_ecb(tenor)
        except Exception as e:  # noqa: BLE001
            print(f"ECB {tenor} failed: {e}")
    if data.get("us2") and data.get("ea2"):
        data["sp2"] = spread(data["us2"], data["ea2"])
    if data.get("us10") and data.get("ea10"):
        data["sp10"] = spread(data["us10"], data["ea10"])

    fx, fx_note = {}, ""
    try:
        fx, fx_note = fetch_fx()
    except Exception as e:  # noqa: BLE001
        print(f"FX failed: {e}")
        fx_note = "курсы недоступны"

    results = {n: analyse_instrument(n, fx[n], data) for n in DISPLAY_ORDER if n in fx}
    basket = usd_basket(fx) if fx else None
    risk = risk_regime(data["vix"], data["spx"]) if data.get("vix") and data.get("spx") else None
    positions = fetch_open_positions()
    try:
        events = fetch_calendar(now)
    except Exception as e:  # noqa: BLE001
        print(f"Calendar failed: {e}")
        events = None   # block is omitted rather than claiming "no events"
    cftc = fetch_cftc()

    prev = load_state()
    message = build_message(now, data, results, basket, risk, positions, fx_note, prev, events, cftc)

    if os.environ.get("DRY_RUN"):
        print(message)
        return
    send_telegram(message)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": now.strftime("%Y-%m-%d"),
                   "labels": {n: r["label"] for n, r in results.items()}}, f, ensure_ascii=False, indent=2)
    print("Macro context sent.")


if __name__ == "__main__":
    sys.exit(main())
