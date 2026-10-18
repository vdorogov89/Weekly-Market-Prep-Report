"""
BTC on-chain signals -> Telegram.

Sends a message ONLY when conditions for a buy or a sell have formed
(plus one short status message per week, so silence never means "broken").

DATA (all free, no API keys):
  Coin Metrics community API : price, MVRV, exchange flows/reserve, active addresses
  DefiLlama                  : total stablecoin supply (for our own SSR)
  Coinbase + OKX public APIs : Coinbase premium (approximation) and OKX open interest
  bitcoin-data.com           : STH/LTH SOPR and STH/LTH realized price,
                               free tier = delayed 7 days, 15 calls/day

TWO TIERS OF SIGNAL
  1) STRATEGIC (cycle level, from MVRV). Checked on 2013-2026 history:
       MVRV < 1.0 -> 180d later price was higher in 82% of cases (median +57%)
       MVRV > 3.0 -> 180d later higher in only 48% (median -2%)
       MVRV 1.0-3.0 -> no useful edge. That is why there is no signal there.
  2) TACTICAL (weeks, from exchange flows + sentiment): a score of 5 indicators.
       Two of them were checked on history (7d netflow, 30d reserve change):
       big outflows -> 14d later up in ~61-64% (median +2.8%), big inflows
       -> up in 44-46%. The edge is modest. The other three (Coinbase
       premium, SSR trend, STH-SOPR) are standard heuristics, NOT verified.

This is an information filter, not a trading system: no entry, stop or
position size. Past statistics do not guarantee the future.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional env: FORCE_STATUS=1 (send the status message now),
              DRY_RUN=1 (print instead of sending, state not saved)
State file: btc_signals_state.json (committed back by the workflow)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_STATUS = os.environ.get("FORCE_STATUS") == "1"

STATE_FILE = "btc_signals_state.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; BtcSignalsBot/1.0)"}
MSK = timezone(timedelta(hours=3))

# --- thresholds ---------------------------------------------------------
MVRV_BUY_ZONE = 1.0          # below: accumulation zone
MVRV_NEAR_BUY = 1.15         # informational "approaching"
MVRV_HEAT = 2.5              # informational "overheating"
MVRV_SELL_ZONE = 3.0         # above: distribution zone

# from the 2018-2026 history: 10th / 90th percentile
NETFLOW7_BUY = -0.84         # % of exchange reserve over 7 days
NETFLOW7_SELL = 0.97
RESERVE30_BUY = -2.0         # % change over 30 days
RESERVE30_SELL = 2.85

PREMIUM_BUY = 0.10           # % (Coinbase vs OKX USDT), average of recent samples
PREMIUM_SELL = -0.10
SSR_CHANGE = 5.0             # % change of our SSR over 30 days
STH_SOPR_BUY = 0.98          # < : short-term holders sell at a loss (capitulation)
STH_SOPR_SELL = 1.05         # > : short-term holders take profit aggressively

TACTICAL_ALERT_SCORE = 3     # of 5; |score| >= this -> alert
TACTICAL_COOLDOWN_H = 72
STRATEGIC_REMIND_DAYS = 14   # re-remind while still inside a zone
MAX_SAMPLES = 400            # premium/OI samples kept in state


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def get_json(url, timeout=25, retries=2, **kw):
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: bad state file, starting fresh: {exc}", file=sys.stderr)
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send_telegram(text):
    if DRY_RUN:
        print("----- DRY RUN MESSAGE -----\n" + text + "\n---------------------------")
        return
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text[:4096]}, timeout=20)
    r.raise_for_status()


def pct(a, b):
    """percent change from b to a."""
    return (a / b - 1) * 100 if b else None


# ---------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------

def fetch_coinmetrics(days=75):
    """Daily rows (oldest -> newest). The newest row may still be 'flash' (partial)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    metrics = "PriceUSD,CapMVRVCur,CapMrktCurUSD,FlowInExNtv,FlowOutExNtv,SplyExNtv,AdrActCnt"
    url = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
           f"?assets=btc&metrics={metrics}&frequency=1d&page_size=200&start_time={start}")
    data = get_json(url, timeout=40)["data"]
    rows = []
    for r in data:
        try:
            rows.append({
                "date": r["time"][:10],
                "price": float(r["PriceUSD"]),
                "mvrv": float(r["CapMVRVCur"]),
                "mcap": float(r["CapMrktCurUSD"]),
                "flow_in": float(r["FlowInExNtv"]),
                "flow_out": float(r["FlowOutExNtv"]),
                "reserve": float(r["SplyExNtv"]),
                "adr": float(r["AdrActCnt"]),
            })
        except (KeyError, ValueError, TypeError):
            continue  # skip incomplete days
    if len(rows) < 35:
        raise RuntimeError(f"Coin Metrics returned too few complete rows: {len(rows)}")
    return rows


def fetch_stablecoin_series():
    """{date: total USD stablecoin supply} from DefiLlama."""
    data = get_json("https://stablecoins.llama.fi/stablecoincharts/all", timeout=40)
    out = {}
    for p in data[-90:]:
        try:
            d = datetime.fromtimestamp(int(p["date"]), tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = float(p["totalCirculatingUSD"]["peggedUSD"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_coinbase_premium():
    """% Coinbase BTC-USD vs OKX BTC-USDT. Approximation of the CryptoQuant index
    (Binance is not reachable from US cloud IPs)."""
    cb = float(get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    okx = float(get_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")["data"][0]["last"])
    return (cb / okx - 1) * 100, cb


def fetch_okx_oi_usd():
    d = get_json("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP")
    return float(d["data"][0]["oiUsd"])


def fetch_bitcoin_data():
    """STH/LTH SOPR + realized prices. Free tier: delayed 7 days, 15 calls/day,
    so the result is cached in state for 20 hours."""
    out = {}
    for key, ep, field in [
        ("sth_sopr", "sth-sopr", "sthSopr"),
        ("lth_sopr", "lth-sopr", "lthSopr"),
        ("sth_rp", "sth-realized-price", "sthRealizedPrice"),
        ("lth_rp", "lth-realized-price", "lthRealizedPrice"),
    ]:
        try:
            j = get_json(f"https://bitcoin-data.com/v1/{ep}/last", retries=0)
            out[key] = float(j[field])
            out["asof"] = j.get("d")
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: bitcoin-data {ep} failed: {exc}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------

def sparse_history(samples, hours):
    cutoff = time.time() - hours * 3600
    return [v for t, v in samples if t >= cutoff]


def analyse(cm, ssr_hist, premium_samples, oi_samples, bd, price_now):
    last = cm[-1]
    mvrv = last["mvrv"]
    nupl = 1 - 1 / mvrv                      # NUPL = (MC - RC) / MC
    realized_price = last["price"] / mvrv    # realized cap / supply

    # Netflow and reserve use the last 7 full days
    # (the newest row can be partial, so it is excluded)
    full = cm[:-1]
    nf7 = sum(r["flow_in"] - r["flow_out"] for r in full[-7:])
    nf7_pct = nf7 / full[-1]["reserve"] * 100
    reserve30 = pct(full[-1]["reserve"], full[-31]["reserve"])
    adr_now = sum(r["adr"] for r in full[-7:]) / 7
    adr_prev = sum(r["adr"] for r in full[-37:-30]) / 7 if len(full) >= 37 else None
    adr_chg = pct(adr_now, adr_prev) if adr_prev else None

    # SSR = BTC market cap / stablecoin supply (our own series; absolute number
    # differs from CryptoQuant's, only the trend is used)
    ssr_now = ssr_chg = None
    d_now, d_old = full[-1]["date"], full[-31]["date"]
    if d_now in ssr_hist and d_old in ssr_hist:
        ssr_now = full[-1]["mcap"] / ssr_hist[d_now]
        ssr_old = full[-31]["mcap"] / ssr_hist[d_old]
        ssr_chg = pct(ssr_now, ssr_old)

    prem_vals = sparse_history(premium_samples, 24)
    premium = sum(prem_vals) / len(prem_vals) if len(prem_vals) >= 3 else None

    oi_chg = None
    old = [v for t, v in oi_samples if time.time() - t >= 6.5 * 86400]
    if old and oi_samples:
        oi_chg = pct(oi_samples[-1][1], old[-1])

    # ---- tactical score: +1 bullish, -1 bearish, 0 neutral/unknown ----
    items = []  # (name, value text, score, tested)

    def add(name, text, score, tested):
        items.append({"name": name, "text": text, "score": score, "tested": tested})

    add("Нетфлоу бирж 7д", f"{nf7:+,.0f} BTC ({nf7_pct:+.2f}% резерва)",
        1 if nf7_pct <= NETFLOW7_BUY else -1 if nf7_pct >= NETFLOW7_SELL else 0, True)
    add("Резерв бирж 30д", f"{reserve30:+.2f}%",
        1 if reserve30 <= RESERVE30_BUY else -1 if reserve30 >= RESERVE30_SELL else 0, True)
    if premium is not None:
        add("Coinbase Premium", f"{premium:+.3f}% (прибл.)",
            1 if premium >= PREMIUM_BUY else -1 if premium <= PREMIUM_SELL else 0, False)
    else:
        add("Coinbase Premium", "копятся замеры", 0, False)
    if ssr_chg is not None:
        # falling SSR = more stablecoin buying power relative to BTC -> bullish
        add("SSR 30д", f"{ssr_chg:+.1f}%",
            1 if ssr_chg <= -SSR_CHANGE else -1 if ssr_chg >= SSR_CHANGE else 0, False)
    else:
        add("SSR 30д", "нет данных", 0, False)
    sth = bd.get("sth_sopr")
    if sth is not None:
        add("STH-SOPR (лаг 7д)", f"{sth:.3f}",
            1 if sth < STH_SOPR_BUY else -1 if sth > STH_SOPR_SELL else 0, False)
    else:
        add("STH-SOPR", "нет данных", 0, False)

    score = sum(i["score"] for i in items)

    # ---- strategic zone from MVRV ----
    if mvrv < MVRV_BUY_ZONE:
        zone = "BUY_ZONE"
    elif mvrv < MVRV_NEAR_BUY:
        zone = "NEAR_BUY"
    elif mvrv > MVRV_SELL_ZONE:
        zone = "SELL_ZONE"
    elif mvrv > MVRV_HEAT:
        zone = "HEAT"
    else:
        zone = "NEUTRAL"

    return {
        "date": last["date"], "price": price_now or last["price"], "mvrv": mvrv,
        "nupl": nupl, "realized_price": realized_price, "zone": zone,
        "score": score, "items": items, "adr_chg": adr_chg, "oi_chg": oi_chg,
        "oi_now": oi_samples[-1][1] if oi_samples else None,
        "sth_rp": bd.get("sth_rp"), "lth_rp": bd.get("lth_rp"),
        "lth_sopr": bd.get("lth_sopr"), "bd_asof": bd.get("asof"),
        "price_chg_7d": pct(last["price"], cm[-8]["price"]) if len(cm) >= 8 else None,
    }


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------

ZONE_TEXT = {
    "BUY_ZONE": "ЗОНА НАКОПЛЕНИЯ: рынок торгуется ниже средней цены покупки всех монет (MVRV < 1)",
    "NEAR_BUY": "приближение к зоне накопления (MVRV < 1.15)",
    "NEUTRAL": "нейтральная зона (MVRV 1.15–2.5): исторически здесь нет устойчивого преимущества",
    "HEAT": "перегрев (MVRV 2.5–3.0): растёт риск, но сигнала на продажу ещё нет",
    "SELL_ZONE": "ЗОНА РАСПРЕДЕЛЕНИЯ: MVRV > 3, исторически зона вершин цикла",
}

DISCLAIMER = ("Это фильтр условий, а не торговая система: нет входа, стопа и объёма. "
              "Проверенные правила: MVRV и нетфлоу/резерв; остальные — стандартные "
              "эвристики без проверки. Прошлая статистика не гарантирует будущее.")


def fmt_usd(x):
    return f"${x:,.0f}"


def indicator_lines(a):
    lines = []
    for i in a["items"]:
        mark = "🟢" if i["score"] > 0 else "🔴" if i["score"] < 0 else "⚪"
        star = "" if i["tested"] else "*"
        lines.append(f"{mark} {i['name']}{star}: {i['text']}")
    return lines


def context_lines(a):
    lines = [
        f"MVRV {a['mvrv']:.2f} · NUPL {a['nupl']:.2f} · Realized Price {fmt_usd(a['realized_price'])}",
    ]
    if a["sth_rp"] and a["lth_rp"]:
        lines.append(f"Себестоимость STH {fmt_usd(a['sth_rp'])} · LTH {fmt_usd(a['lth_rp'])} "
                     f"(данные на {a['bd_asof']})")
        if a["price"] < a["sth_rp"]:
            lines.append("Цена ниже себестоимости краткосрочных держателей: они в убытке.")
    if a["lth_sopr"] is not None:
        lines.append(f"LTH-SOPR {a['lth_sopr']:.2f} (>2.5 — долгосрочные массово фиксируют прибыль)")
    if a["adr_chg"] is not None:
        lines.append(f"Активные адреса, 7д к предыдущему месяцу: {a['adr_chg']:+.1f}%")
    if a["oi_chg"] is not None:
        lines.append(f"Open Interest OKX BTC-USDT за 7д: {a['oi_chg']:+.1f}% (только одна биржа)")
    return lines


def build_status(a):
    now = datetime.now(MSK).strftime("%d.%m %H:%M МСК")
    head = [f"📊 BTC · недельный статус · {now}",
            f"Цена {fmt_usd(a['price'])}"
            + (f" ({a['price_chg_7d']:+.1f}% за 7д)" if a["price_chg_7d"] is not None else ""),
            "", "Режим: " + ZONE_TEXT[a["zone"]],
            f"Тактический счёт: {a['score']:+d} из ±5 (сигнал при |счёта| ≥ {TACTICAL_ALERT_SCORE})",
            ""]
    body = indicator_lines(a) + [""] + context_lines(a)
    tail = ["", "Сигнала на покупку/продажу сейчас нет." if abs(a["score"]) < TACTICAL_ALERT_SCORE
            and a["zone"] in ("NEUTRAL", "HEAT", "NEAR_BUY") else "Смотри отдельные сигналы выше.",
            "* — не проверено на истории.", ]
    return "\n".join(head + body + tail)


def build_strategic(a, kind):
    if kind == "BUY_ZONE":
        title = "🟢🟢 BTC · СТРАТЕГИЧЕСКИЙ СИГНАЛ: зона накопления"
        stat = ("История 2013–2026: при MVRV < 1 через 180 дней цена была выше в 82% случаев "
                "(медиана +57%), но худший исход −33%. Рынок может оставаться в зоне месяцами, "
                "поэтому входят частями, а не одной сделкой.")
    elif kind == "SELL_ZONE":
        title = "🔴🔴 BTC · СТРАТЕГИЧЕСКИЙ СИГНАЛ: зона распределения"
        stat = ("История 2013–2026: при MVRV > 3 через 180 дней цена была выше лишь в 48% случаев "
                "(медиана −2%, худший −67%). Это зона, где разумно сокращать риск, но вершина "
                "может растянуться на недели.")
    else:
        title = "🟠 BTC · перегрев (MVRV 2.5–3.0)"
        stat = "Это предупреждение о росте риска, а не сигнал на продажу."
    lines = [title, f"Цена {fmt_usd(a['price'])} · {a['date']}", "", stat, ""]
    lines += context_lines(a) + [""] + indicator_lines(a)
    lines += ["", f"Тактический счёт: {a['score']:+d} из ±5", "", DISCLAIMER]
    return "\n".join(lines)


def build_tactical(a, side):
    if side == "BUY":
        title = "🟢 BTC · складываются условия для ПОКУПКИ (тактический)"
        what = ("Монеты уходят с бирж, давление предложения снижается. "
                "На истории (2018–2026) сильный отток с бирж давал рост через 14 дней в ~61–64% "
                "случаев, медиана около +3%. Преимущество скромное.")
    else:
        title = "🔴 BTC · складываются условия для ПРОДАЖИ / сокращения (тактический)"
        what = ("Монеты притекают на биржи, давление продавцов растёт. На истории (2018–2026) "
                "сильный приток на биржи давал рост через 14 дней лишь в ~44–46% случаев, "
                "медиана около −1.6%. Преимущество скромное.")
    zone_note = "Фон: " + ZONE_TEXT[a["zone"]]
    lines = [title, f"Цена {fmt_usd(a['price'])} · {a['date']}", "",
             f"Счёт {a['score']:+d} из ±5", ""]
    lines += indicator_lines(a) + ["", what, "", zone_note, ""]
    lines += context_lines(a) + ["", "* — не проверено на истории.", DISCLAIMER]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    state = load_state()
    now = time.time()
    now_utc = datetime.now(timezone.utc)

    cm = fetch_coinmetrics()

    try:
        ssr_hist = fetch_stablecoin_series()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: stablecoin data unavailable: {exc}", file=sys.stderr)
        ssr_hist = {}

    premium_samples = state.get("premium_samples", [])
    price_now = None
    try:
        prem, price_now = fetch_coinbase_premium()
        premium_samples.append([now, prem])
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: premium unavailable: {exc}", file=sys.stderr)
    oi_samples = state.get("oi_samples", [])
    try:
        # at most one OI sample per 6 hours is enough
        if not oi_samples or now - oi_samples[-1][0] > 6 * 3600:
            oi_samples.append([now, fetch_okx_oi_usd()])
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: OI unavailable: {exc}", file=sys.stderr)
    premium_samples = premium_samples[-MAX_SAMPLES:]
    oi_samples = [s for s in oi_samples if now - s[0] < 20 * 86400][-MAX_SAMPLES:]

    cache = state.get("bd_cache", {})
    if cache.get("ts") and now - cache["ts"] < 20 * 3600 and cache.get("data"):
        bd = cache["data"]
    else:
        bd = fetch_bitcoin_data()
        if bd:
            cache = {"ts": now, "data": bd}
        elif cache.get("data"):
            bd = cache["data"]  # use stale rather than nothing

    a = analyse(cm, ssr_hist, premium_samples, oi_samples, bd, price_now)
    print(f"Date {a['date']} price {a['price']:.0f} MVRV {a['mvrv']:.3f} zone {a['zone']} score {a['score']}")
    for i in a["items"]:
        print(f"  {i['name']}: {i['text']} -> {i['score']}")

    alerts = state.get("alerts", {})   # key -> last sent epoch
    messages = []                      # (alert key or None, text)

    # 1) strategic zones: alert on entering, remind every N days while inside
    zone = a["zone"]
    prev_zone = state.get("zone")
    if zone in ("BUY_ZONE", "SELL_ZONE", "HEAT"):
        last_sent = alerts.get("zone:" + zone, 0)
        entered = prev_zone != zone
        reminder_due = now - last_sent > STRATEGIC_REMIND_DAYS * 86400
        if (entered and now - last_sent > 3 * 86400) or reminder_due:
            messages.append(("zone:" + zone, build_strategic(a, zone)))

    # 2) tactical
    side = "BUY" if a["score"] >= TACTICAL_ALERT_SCORE else "SELL" if a["score"] <= -TACTICAL_ALERT_SCORE else None
    if side and now - alerts.get("tactical:" + side, 0) > TACTICAL_COOLDOWN_H * 3600:
        messages.append(("tactical:" + side, build_tactical(a, side)))

    # 3) weekly status: Sunday, once (also on demand)
    iso_week = now_utc.strftime("%G-%V")
    if FORCE_STATUS or (now_utc.weekday() == 6 and state.get("status_week") != iso_week):
        messages.append(("status", build_status(a)))

    failed = False
    for key, text in messages:
        try:
            send_telegram(text)
            if key == "status":
                state["status_week"] = iso_week
            else:
                alerts[key] = now
            print(f"Sent: {key}")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"Warning: send failed for {key}: {exc}", file=sys.stderr)

    if not messages:
        print("No alert: conditions not met.")

    if DRY_RUN:
        return
    state.update({
        "zone": zone, "alerts": alerts, "premium_samples": premium_samples,
        "oi_samples": oi_samples, "bd_cache": cache,
        "last_run": now_utc.isoformat(), "last_score": a["score"], "last_mvrv": round(a["mvrv"], 4),
    })
    save_state(state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
