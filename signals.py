"""
BTC market top/bottom signals -> Discord.

Checks every SIGNAL_SECONDS (default 300); alerts when a signal turns ON/OFF,
a special alert when >= 3 signals of the same group are on, a daily summary at
DAILY_HOUR_UTC and a message whenever a new day of US spot ETF flows is published.

Sources (Binance/Bybit block US IPs, i.e. GitHub runners):
  Hyperliquid + OKX (funding, OI, retail long/short), Coinbase vs OKX (premium),
  alternative.me (Fear & Greed), ETF flows: SoSoValue if SOSO_API_KEY is set,
  otherwise CoinMarketCap's public chart data.

Webhook: DISCORD_SIGNALS_WEBHOOK_URL (falls back to DISCORD_WEBHOOK_URL).
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
CONFIRM = 2          # consecutive checks needed to flip a signal
SLOW_EVERY = 1800    # ETF + Fear&Greed refresh
VERSION = 2

BOTTOM = {
    "etf_in": "ETFs: 2+ dias seguidos de entradas",
    "funding_neg": "Funding negativo",
    "oi_flush": "Desalavancagem: OI −8% vs máx 24h",
    "ls_low": "Retalho a sair de long (L/S < 2.5)",
    "prem_pos": "Coinbase premium > +0.05%",
    "fng_low": "Fear & Greed ≤ 20 (medo extremo)",
}
TOP = {
    "etf_out": "ETFs: 3+ dias seguidos de saídas",
    "funding_hot": "Funding sobreaquecido (≥ +0.03%/8h)",
    "oi_surge": "Alavancagem a subir: OI +10% vs mín 24h",
    "ls_high": "Retalho eufórico (L/S > 4)",
    "prem_neg": "Coinbase premium < −0.15% (EUA a vender)",
    "fng_high": "Fear & Greed ≥ 80 (ganância extrema)",
}
GROUPS = (("🟢 FUNDO", BOTTOM), ("🔴 TOPO/PRESSÃO VENDEDORA", TOP))


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
                    "px": float(c["markPx"]), "prev": float(c["prevDayPx"])}
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


def fear_greed():
    d = _get("https://api.alternative.me/fng/?limit=1")["data"][0]
    return int(d["value"]), d["value_classification"]


def etf_flows():
    """[(YYYY-MM-DD, net USD)] newest first."""
    key = os.environ.get("SOSO_API_KEY")
    if key:
        d = _post("https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart",
                  {"type": "us-btc-spot"}, {"x-soso-api-key": key})
        rows = d.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or []
        out = [(r["date"][:10], float(r["totalNetInflow"])) for r in rows]
        src = "SoSoValue"
    else:
        pts = _get("https://api.coinmarketcap.com/data-api/v3/etf/overview/netflow/chart"
                   "?category=btc&range=30d")["data"]["points"]
        out = [(time.strftime("%Y-%m-%d", time.gmtime(int(p["timestamp"]) / 1000)), float(p["btcValue"]))
               for p in pts]
        src = "CoinMarketCap"
    out.sort(reverse=True)
    return out[:10], src


# ---------------------------------------------------------------- formatting
def usd(x):
    s = "-" if x < 0 else "+"
    x = abs(x)
    return f"{s}${x/1e9:.2f}B" if x >= 1e9 else f"{s}${x/1e6:.1f}M"


def streak(flows, positive):
    n = 0
    for _, v in flows:
        if (v > 0) != positive or v == 0:
            break
        n += 1
    return n


def evaluate(st, errors):
    """Return {signal: (bool|None, text)}; keeps rolling history in st."""
    now = time.time()
    res = {}
    hl = _safe(hl_btc, errors, "hyperliquid")
    if hl:
        st["px"], st["chg"] = hl["px"], (hl["px"] / hl["prev"] - 1) * 100

    # funding
    of = _safe(okx_funding, errors, "okx funding")
    hf = hl["funding8h"] if hl else None
    rates = [r for r in (of, hf) if r is not None]
    if rates:
        txt = " · ".join(f"{n} {r:+.4f}%" for n, r in (("OKX", of), ("HL", hf)) if r is not None) + " /8h"
        res["funding_neg"] = (all(r < 0 for r in rates), txt)
        res["funding_hot"] = (all(r >= 0.03 for r in rates), txt)

    # open interest in coins (OKX + HL), 24h window
    oo = _safe(okx_oi, errors, "okx oi")
    if hl and oo is not None:
        oi = hl["oi"] + oo
        hist = [h for h in st.get("oi_hist", []) if now - h[0] < 24 * 3600] + [[now, oi]]
        st["oi_hist"] = hist
        hi, lo = max(h[1] for h in hist), min(h[1] for h in hist)
        dd, du = (oi / hi - 1) * 100, (oi / lo - 1) * 100
        enough = now - hist[0][0] > 6 * 3600
        tail = "" if enough else " · a recolher histórico"
        res["oi_flush"] = (dd <= -8 if enough else None, f"{oi:,.0f} BTC ({dd:+.1f}% vs máx 24h){tail}")
        res["oi_surge"] = (du >= 10 if enough else None, f"{oi:,.0f} BTC ({du:+.1f}% vs mín 24h){tail}")

    # retail long/short
    ls = _safe(okx_ls, errors, "okx l/s")
    if ls is not None:
        res["ls_low"] = (ls < 2.5, f"{ls:.2f}")
        res["ls_high"] = (ls > 4.0, f"{ls:.2f}")

    # coinbase premium, 30-min average
    p = _safe(premium, errors, "premium")
    if p is not None:
        ph = [h for h in st.get("prem_hist", []) if now - h[0] < 1800] + [[now, p]]
        st["prem_hist"] = ph
        avg = sum(h[1] for h in ph) / len(ph)
        res["prem_pos"] = (avg > 0.05, f"{avg:+.3f}% (média 30 min)")
        res["prem_neg"] = (avg < -0.15, f"{avg:+.3f}% (média 30 min)")

    # slow data: ETF flows + Fear & Greed
    if now - st.get("slow_ts", 0) > SLOW_EVERY:
        st["slow_ts"] = now
        fg = _safe(fear_greed, errors, "fear&greed")
        if fg:
            st["fng"] = fg
        fl = _safe(etf_flows, errors, "etf")
        if fl and fl[0]:
            st["etf"], st["etf_src"] = fl
    if st.get("fng"):
        v, cls = st["fng"]
        res["fng_low"] = (v <= 20, f"{v} ({cls})")
        res["fng_high"] = (v >= 80, f"{v} ({cls})")
    fl = st.get("etf")
    if fl:
        last = " · ".join(f"{d[5:]} {usd(v)}" for d, v in fl[:3])
        res["etf_in"] = (streak(fl, True) >= 2, last)
        res["etf_out"] = (streak(fl, False) >= 3, last)
    return res


def status_embed(title, st, color, desc=None):
    on, vals = st.get("on", {}), st.get("vals", {})
    parts = [desc] if desc else []
    counts = []
    for gname, sigs in GROUPS:
        n = sum(1 for k in sigs if on.get(k))
        counts.append(f"{gname.split()[1].split('/')[0].lower()} {n}/{len(sigs)}")
        lines = [f"**{gname}: {n}/{len(sigs)}**"]
        for k, name in sigs.items():
            v = on.get(k)
            icon = "✅" if v else ("⚪" if v is None else "▫️")
            lines.append(f"{icon} {name}: `{vals.get(k, '—')}`")
        parts.append("\n".join(lines))
    px = st.get("px") or 0
    return {
        "title": title,
        "color": color,
        "description": "\n\n".join(parts)[:4000],
        "footer": {"text": f"BTC {px:,.0f} ({st.get('chg', 0):+.1f}% 24h) · " + " · ".join(counts)
                           + " · não é conselho financeiro"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def check(state, send, log):
    st = state.get("_signals") or {}
    if st.get("v") != VERSION:  # migrate: keep histories, re-announce
        st = {k: st[k] for k in ("oi_hist", "prem_hist") if k in st}
        st["v"] = VERSION
    state["_signals"] = st
    now = time.time()
    if now - st.get("last_run", 0) < EVERY:
        return
    st["last_run"] = now
    errors = []
    res = evaluate(st, errors)
    if errors:
        log("signals errors:", "; ".join(errors))

    url = os.environ.get("DISCORD_SIGNALS_WEBHOOK_URL") or None

    def out(embeds):
        send(embeds, "Market Signals", url)

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

    if first:
        st["init"] = True
        out([status_embed("📡 Alertas de topos e fundos BTC ativos", st, 0x95A5A6,
                          "Aviso quando cada sinal liga/desliga, alerta 🚨 com 3+ sinais do mesmo lado, "
                          "resumo diário às 08h UTC e fluxos de ETF de cada dia.")])
    for k, val in events:
        is_bottom = k in BOTTOM
        group, sigs = ("FUNDO", BOTTOM) if is_bottom else ("TOPO", TOP)
        n = sum(1 for s in sigs if on.get(s))
        if val:
            title = f"{'🟢' if is_bottom else '🔴'} Sinal de {group} ligou: {sigs[k]}"
            color = 0x2ECC71 if is_bottom else 0xE74C3C
            if n >= 3:
                title = f"🚨 {n}/{len(sigs)} SINAIS DE {group} ATIVOS ({sigs[k]} ligou)"
                color = 0xF1C40F
        else:
            title, color = f"⚪ Sinal de {group} desligou: {sigs[k]}", 0x7F8C8D
        out([status_embed(title, st, color)])

    # new ETF day published
    fl = st.get("etf")
    if fl and st.get("etf_seen") != fl[0][0]:
        new_day = st.get("etf_seen") is not None
        st["etf_seen"] = fl[0][0]
        if new_day:
            d, v = fl[0]
            si, so = streak(fl, True), streak(fl, False)
            seq = f"{si}º dia seguido de entradas" if si else f"{so}º dia seguido de saídas"
            out([{
                "title": f"🏦 ETFs BTC (EUA) {d}: {usd(v)}",
                "color": 0x2ECC71 if v > 0 else 0xE74C3C,
                "description": f"{seq}\n" + "\n".join(f"`{x[5:]}` {usd(y)}" for x, y in fl[:7]),
                "footer": {"text": f"fonte: {st.get('etf_src', '?')} · valores preliminares podem ser revistos"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }])

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if not first and time.gmtime().tm_hour == DAILY_HOUR_UTC and st.get("daily") != today:
        st["daily"] = today
        out([status_embed("☀️ Resumo diário: topos e fundos BTC", st, 0x3498DB)])
