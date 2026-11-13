"""
Market "bottom signals" for BTC -> Discord.

Checks every SIGNAL_SECONDS (default 300) and alerts when a signal turns ON/OFF:
  1. ETF      US spot BTC ETFs with >= 2 consecutive days of net inflows (needs SOSO_API_KEY)
  2. Funding  BTC funding negative on OKX and Hyperliquid
  3. OI       BTC open interest (OKX + Hyperliquid, in coins) >= 8% below its 24h high
  4. L/S      OKX retail long/short account ratio < 2.5
  5. Premium  Coinbase BTC-USD premium vs OKX BTC-USDT > +0.05% (30-min average)

Binance/Bybit block US IPs (GitHub runners), so OKX + Hyperliquid + Coinbase are used.
A signal must hold for 2 consecutive checks to flip (avoids flapping).
Daily status summary at DAILY_HOUR_UTC.
"""
import json
import os
import time
import urllib.request

HL = "https://api.hyperliquid.xyz/info"
OKX = "https://www.okx.com/api/v5"
UA = {"Content-Type": "application/json", "User-Agent": "hl-tracker/1.0"}

EVERY = float(os.environ.get("SIGNAL_SECONDS", 300))
DAILY_HOUR_UTC = int(os.environ.get("DAILY_HOUR_UTC", 8))
OI_DROP_PCT = 8.0
LS_MAX = 2.5
PREMIUM_MIN = 0.05      # %
ETF_DAYS = 2
CONFIRM = 2             # consecutive checks needed to flip a signal

NAMES = {
    "etf": "Entradas nos ETFs",
    "funding": "Funding negativo",
    "oi": "Desalavancagem (OI a cair)",
    "ls": "Retalho a sair de long",
    "premium": "Coinbase premium positivo",
}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.loads(r.read())


def _post(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _safe(fn, errors, name):
    try:
        return fn()
    except Exception as e:
        errors.append(f"{name}: {e}")
        return None


# ---------------------------------------------------------------- fetchers
def hl_btc():
    meta, ctxs = _post(HL, {"type": "metaAndAssetCtxs"})
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] == "BTC":
            return {"funding8h": float(c["funding"]) * 8 * 100, "oi": float(c["openInterest"]),
                    "px": float(c["markPx"])}
    raise ValueError("BTC not found")


def okx_funding():
    return float(_get(f"{OKX}/public/funding-rate?instId=BTC-USDT-SWAP")["data"][0]["fundingRate"]) * 100


def okx_oi():
    return float(_get(f"{OKX}/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP")["data"][0]["oiCcy"])


def okx_ls():
    return float(_get(f"{OKX}/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H")["data"][0][1])


def premium():
    cb = float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    ok = float(_get(f"{OKX}/market/ticker?instId=BTC-USDT")["data"][0]["last"])
    return (cb / ok - 1) * 100


def etf_flows():
    """Last daily net inflows (USD), newest first. SoSoValue free API key required."""
    key = os.environ.get("SOSO_API_KEY")
    if not key:
        return None
    d = _post("https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart",
              {"type": "us-btc-spot"}, {"x-soso-api-key": key})
    rows = d.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or []
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    return [(r["date"], float(r["totalNetInflow"])) for r in rows[:7]]


# ---------------------------------------------------------------- formatting
def usd(x):
    s = "-" if x < 0 else "+"
    x = abs(x)
    return f"{s}${x/1e9:.2f}B" if x >= 1e9 else f"{s}${x/1e6:.1f}M"


def evaluate(st, errors):
    """Return {signal: (bool|None, text)}; mutates st (history)."""
    now = time.time()
    res = {}
    hl = _safe(hl_btc, errors, "hyperliquid")
    st["px"] = hl["px"] if hl else st.get("px")

    # funding
    of = _safe(okx_funding, errors, "okx funding")
    rates = [r for r in (of, hl and hl["funding8h"]) if r is not None]
    if rates:
        txt = " · ".join(f"{n} {r:+.4f}%" for n, r in (("OKX", of), ("HL", hl and hl["funding8h"])) if r is not None)
        res["funding"] = (all(r < 0 for r in rates), txt + " /8h")

    # open interest (coins)
    oo = _safe(okx_oi, errors, "okx oi")
    if hl and oo is not None:
        oi = hl["oi"] + oo
        hist = [h for h in st.get("oi_hist", []) if now - h[0] < 24 * 3600]
        hist.append([now, oi])
        st["oi_hist"] = hist
        hi = max(h[1] for h in hist)
        drop = (oi / hi - 1) * 100
        enough = now - hist[0][0] > 6 * 3600  # need some history before judging
        res["oi"] = (drop <= -OI_DROP_PCT if enough else None,
                     f"{oi:,.0f} BTC ({drop:+.1f}% vs máx 24h)" + ("" if enough else " · a recolher histórico"))

    # long/short
    ls = _safe(okx_ls, errors, "okx l/s")
    if ls is not None:
        res["ls"] = (ls < LS_MAX, f"{ls:.2f} (alvo < {LS_MAX})")

    # coinbase premium, 30-min average
    p = _safe(premium, errors, "premium")
    if p is not None:
        ph = [h for h in st.get("prem_hist", []) if now - h[0] < 1800] + [[now, p]]
        st["prem_hist"] = ph
        avg = sum(h[1] for h in ph) / len(ph)
        res["premium"] = (avg > PREMIUM_MIN, f"{avg:+.3f}% (média 30 min)")

    # ETFs (refresh at most every 30 min)
    if os.environ.get("SOSO_API_KEY"):
        if now - st.get("etf_ts", 0) > 1800:
            fl = _safe(etf_flows, errors, "etf")
            if fl:
                st["etf"], st["etf_ts"] = fl, now
        fl = st.get("etf")
        if fl:
            streak = 0
            for _, v in fl:
                if v <= 0:
                    break
                streak += 1
            last = " · ".join(f"{d[5:]} {usd(v)}" for d, v in fl[:3])
            res["etf"] = (streak >= ETF_DAYS, f"{last}")
    else:
        res["etf"] = (None, "sem API key (SOSO_API_KEY)")
    return res


def status_embed(title, st, color, desc=None):
    on = st.get("on", {})
    vals = st.get("vals", {})
    lines = []
    for k, name in NAMES.items():
        v = on.get(k)
        icon = "✅" if v else ("⚪" if v is None else "❌")
        lines.append(f"{icon} **{name}**: {vals.get(k, '—')}")
    n = sum(1 for v in on.values() if v)
    e = {
        "title": title,
        "color": color,
        "description": ((desc + "\n\n") if desc else "") + "\n".join(lines),
        "footer": {"text": f"BTC {st.get('px', 0):,.0f} · {n}/5 sinais de fundo ativos · não é conselho financeiro"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return e


def check(state, send, log):
    st = state.setdefault("_signals", {})
    now = time.time()
    if now - st.get("last_run", 0) < EVERY:
        return
    st["last_run"] = now
    errors = []
    res = evaluate(st, errors)
    if errors:
        log("signals errors:", "; ".join(errors))

    on, pend = st.setdefault("on", {}), st.setdefault("pending", {})
    st.setdefault("vals", {}).update({k: t for k, (_, t) in res.items()})
    first = not st.get("init")
    events = []
    for k, (val, _) in res.items():
        if val is None:
            on.setdefault(k, None)
            pend.pop(k, None)
            continue
        if first or on.get(k) is None:
            on[k] = val
            continue
        if val != on[k]:
            pend[k] = pend.get(k, 0) + 1
            if pend[k] >= CONFIRM:
                on[k], pend[k] = val, 0
                events.append((k, val))
        else:
            pend[k] = 0

    n = sum(1 for v in on.values() if v)
    if first:
        st["init"] = True
        send([status_embed("📡 Alertas de sinais de fundo ativos", st, 0x95A5A6,
                           "Vou avisar quando algum destes sinais ligar ou desligar.")], "Market Signals")
    for k, val in events:
        if val:
            title, color = f"🟢 Sinal de fundo LIGOU: {NAMES[k]}", 0x2ECC71
        else:
            title, color = f"🔴 Sinal de fundo desligou: {NAMES[k]}", 0xE74C3C
        desc = None
        if val and n >= 3:
            title = f"🚨 {n}/5 SINAIS DE FUNDO ATIVOS: {NAMES[k]} ligou"
            color = 0xF1C40F
        send([status_embed(title, st, color, desc)], "Market Signals")

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if (not first and time.gmtime().tm_hour == DAILY_HOUR_UTC and st.get("daily") != today):
        st["daily"] = today
        send([status_embed("☀️ Resumo diário: sinais de fundo BTC", st, 0x3498DB)], "Market Signals")
