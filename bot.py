"""
Hyperliquid whale tracker -> Discord webhook.

Compares position snapshots of each wallet (main perps + every HIP-3 dex)
every POLL_SECONDS and posts an alert when a position is opened, closed,
flipped, or changes size significantly. Only uses the Python stdlib.

Config:
  .env            DISCORD_WEBHOOK_URL=...   (optional POLL_SECONDS=30)
  wallets.json    list of wallets + thresholds (hot-reloaded every cycle)
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = os.path.join(BASE, "wallets.json")
STATE_FILE = os.path.join(BASE, "state.json")
UA = {"Content-Type": "application/json", "User-Agent": "hl-tracker/1.0"}

DEFAULTS = {
    "min_position_usd": 25_000,  # ignore open/close of positions smaller than this
    "min_change_usd": 100_000,   # size change must be >= this notional...
    "min_change_pct": 20,        # ...AND >= this % of the previous position
    "cooldown_min": 10,          # min minutes between size-change alerts on the same coin
}


def load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- HTTP
def post_json(url, payload, retries=4):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt * 2
                try:
                    wait = max(wait, float(json.loads(e.read()).get("retry_after", 0)) + 0.5)
                except Exception:
                    pass
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


def info(payload):
    return post_json(API, payload)


# ---------------------------------------------------------------- data
_dexes = {"list": [""], "ts": 0}


def dexes():
    if time.time() - _dexes["ts"] > 3600:
        try:
            d = info({"type": "perpDexs"})
            _dexes["list"] = [""] + [x["name"] for x in d if x]
            _dexes["ts"] = time.time()
        except Exception as e:
            log("perpDexs failed:", e)
    return _dexes["list"]


def snapshot(addr):
    """Return {coin: position dict} over all dexes, plus total account value."""
    positions, equity = {}, 0.0

    def one(dex):
        p = {"type": "clearinghouseState", "user": addr}
        if dex:
            p["dex"] = dex
        return info(p)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(one, dexes()))
    for st in results:
        equity += float(st["marginSummary"]["accountValue"])
        for ap in st["assetPositions"]:
            p = ap["position"]
            szi = float(p["szi"])
            if szi == 0:
                continue
            positions[p["coin"]] = {
                "szi": szi,
                "entry": float(p["entryPx"] or 0),
                "value": float(p["positionValue"]),
                "upnl": float(p["unrealizedPnl"]),
                "lev": p["leverage"]["value"],
                "lev_type": p["leverage"]["type"],
                "liq": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
            }
    return positions, equity


def fills_since(addr, start_ms):
    try:
        return info({"type": "userFillsByTime", "user": addr,
                     "startTime": int(start_ms), "aggregateByTime": True}) or []
    except Exception as e:
        log("fills failed:", e)
        return []


# ---------------------------------------------------------------- formatting
def usd(x, sign=False):
    s = "-" if x < 0 else ("+" if sign else "")
    x = abs(x)
    if x >= 1e9:
        return f"{s}${x/1e9:.2f}B"
    if x >= 1e6:
        return f"{s}${x/1e6:.2f}M"
    if x >= 1e3:
        return f"{s}${x/1e3:.1f}k"
    return f"{s}${x:,.0f}"


def px(x):
    if x is None:
        return "—"
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:,.4g}"
    return f"{x:.6g}"


def side(szi):
    return "LONG" if szi > 0 else "SHORT"


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}"


def wallet_label(w):
    return f"{w['name']} ({short(w['address'])})" if w.get("name") else short(w["address"])


def link(addr):
    return f"https://hypurrscan.io/address/{addr}"


COLORS = {"open": 0x2ECC71, "close": 0xE74C3C, "inc": 0x3498DB, "dec": 0xF39C12,
          "flip": 0x9B59B6, "liq": 0x000000, "info": 0x95A5A6}


def pos_fields(p):
    f = [
        ("Tamanho", f"{abs(p['szi']):,.4g} ({usd(p['value'])})"),
        ("Entrada", px(p["entry"])),
        ("Alavancagem", f"{p['lev']}x {p['lev_type']}"),
        ("uPnL", usd(p["upnl"], sign=True)),
    ]
    if p.get("liq"):
        f.append(("Liquidação", px(p["liq"])))
    return f


def embed(kind, title, w, fields, desc=None):
    e = {
        "title": title,
        "url": link(w["address"]),
        "color": COLORS[kind],
        "author": {"name": wallet_label(w)},
        "fields": [{"name": n, "value": v, "inline": True} for n, v in fields],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if desc:
        e["description"] = desc
    return e


# ---------------------------------------------------------------- diff
def diff(w, old, new, fills, prev, last_alert):
    """old = positions as last reported (baseline), prev = previous poll.
    Mutates old/last_alert so the baseline follows what was reported."""
    th = {**DEFAULTS, **{k: w[k] for k in DEFAULTS if k in w}}
    now = time.time()
    by_coin = {}
    for f in fills:
        by_coin.setdefault(f["coin"], []).append(f)

    def closed_info(coin):
        fs = [f for f in by_coin.get(coin, []) if "Close" in f["dir"] or "Liquidat" in f["dir"]]
        pnl = sum(float(f["closedPnl"]) for f in fs)
        sz = sum(float(f["sz"]) for f in fs)
        avg = sum(float(f["px"]) * float(f["sz"]) for f in fs) / sz if sz else None
        liq = any("Liquidat" in f["dir"] or f.get("liquidation") for f in fs)
        return pnl, avg, liq, bool(fs)

    out = []

    def sync(coin):
        if coin in new:
            old[coin] = new[coin]
        else:
            old.pop(coin, None)

    for coin in sorted(set(old) | set(new)):
        o, n = old.get(coin), new.get(coin)
        if o and n and (o["szi"] > 0) == (n["szi"] > 0):
            # same side: size change
            d_sz = abs(n["szi"]) - abs(o["szi"])
            d_usd = abs(d_sz) * (n["value"] / abs(n["szi"]))
            pct = abs(d_sz) / abs(o["szi"]) * 100
            if d_usd < th["min_change_usd"] or pct < th["min_change_pct"]:
                old[coin] = {**n, "szi": o["szi"]}  # keep baseline size, refresh other fields
                continue
            if now - last_alert.get(coin, 0) < th["cooldown_min"] * 60:
                continue
            last_alert[coin] = now
            sync(coin)
            if d_sz > 0:
                out.append(embed("inc", f"➕ Aumentou {side(n['szi'])} {coin} (+{pct:.0f}%)", w,
                                 [("Variação", f"+{abs(d_sz):,.4g} ({usd(d_usd)})")] + pos_fields(n)))
            else:
                pnl, avg, liq, has = closed_info(coin)
                extra = [("Variação", f"-{abs(d_sz):,.4g} ({usd(d_usd)})")]
                if has:
                    extra.append(("PnL realizado", usd(pnl, sign=True)))
                kind, verb = ("liq", "💀 Liquidado parcialmente") if liq else ("dec", "➖ Reduziu")
                out.append(embed(kind, f"{verb} {side(n['szi'])} {coin} (-{pct:.0f}%)", w,
                                 extra + pos_fields(n)))
        elif o and n:
            # flipped side
            sync(coin)
            last_alert[coin] = now
            if max(abs(o["value"]), abs(n["value"])) < th["min_position_usd"]:
                continue
            pnl, avg, liq, has = closed_info(coin)
            desc = f"Era {side(o['szi'])} {usd(o['value'])}"
            if has:
                desc += f" · PnL realizado {usd(pnl, sign=True)}"
            out.append(embed("flip", f"🔄 Inverteu {coin}: {side(o['szi'])} → {side(n['szi'])}", w,
                             pos_fields(n), desc))
        elif n:
            sync(coin)
            last_alert[coin] = now
            if n["value"] < th["min_position_usd"]:
                continue
            emoji = "🟢" if n["szi"] > 0 else "🔴"
            out.append(embed("open", f"{emoji} Abriu {side(n['szi'])} {coin}", w, pos_fields(n)))
        else:
            sync(coin)
            o = prev.get(coin, o)  # freshest uPnL before close
            if o["value"] < th["min_position_usd"]:
                continue
            pnl, avg, liq, has = closed_info(coin)
            fields = [("Tamanho", f"{abs(o['szi']):,.4g} ({usd(o['value'])})"),
                      ("Entrada", px(o["entry"]))]
            if avg:
                fields.append(("Saída (média)", px(avg)))
            fields.append(("PnL realizado" if has else "PnL (último uPnL)",
                           usd(pnl if has else o["upnl"], sign=True)))
            kind, verb = ("liq", "💀 LIQUIDADO") if liq else ("close", "✅ Fechou")
            out.append(embed(kind, f"{verb} {side(o['szi'])} {coin}", w, fields))
    return out


def summary_embed(w, pos, equity, title):
    top = sorted(pos.items(), key=lambda kv: -kv[1]["value"])
    lines = []
    for coin, p in top[:20]:
        lines.append(f"`{side(p['szi']):5}` **{coin}** {usd(p['value'])} @ {px(p['entry'])} "
                     f"· {p['lev']}x · uPnL {usd(p['upnl'], sign=True)}")
    if len(top) > 20:
        lines.append(f"… e mais {len(top) - 20} posições")
    total = sum(p["value"] for p in pos.values())
    upnl = sum(p["upnl"] for p in pos.values())
    return embed("info", title, w,
                 [("Equity", usd(equity)), ("Exposição", usd(total)),
                  ("uPnL total", usd(upnl, sign=True)), ("Posições", str(len(pos)))],
                 "\n".join(lines) or "Sem posições abertas.")


# ---------------------------------------------------------------- discord
def send(embeds):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        for e in embeds:
            log("[no webhook]", e["title"], "|", e.get("description", "")[:200],
                "|", "; ".join(f"{f['name']}={f['value']}" for f in e["fields"]))
        return
    for i in range(0, len(embeds), 10):  # max 10 embeds per webhook message
        try:
            post_json(url, {"username": "Whale Tracker", "embeds": embeds[i:i + 10]})
        except Exception as e:
            log("discord send failed:", e)
        time.sleep(1)


# ---------------------------------------------------------------- main loop
def load_wallets():
    try:
        ws = json.load(open(WALLETS_FILE, encoding="utf-8"))
        for w in ws:
            w["address"] = w["address"].lower()
        return [w for w in ws if w.get("enabled", True)]
    except Exception as e:
        log("wallets.json error:", e)
        return None


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, STATE_FILE)


def main():
    load_env()
    poll = float(os.environ.get("POLL_SECONDS", 30))
    run_for = float(os.environ.get("RUN_SECONDS", 0))  # 0 = forever (VPS); >0 = CI runs
    started = time.time()
    state = load_state()
    wallets = []
    log(f"started, poll={poll}s, webhook={'yes' if os.environ.get('DISCORD_WEBHOOK_URL') else 'NO (console only)'}")
    while not run_for or time.time() - started < run_for:
        t0 = time.time()
        wallets = load_wallets() or wallets
        for w in wallets:
            addr = w["address"]
            try:
                now_ms = int(time.time() * 1000)
                pos, equity = snapshot(addr)
                prev = state.get(addr)
                if prev is None:
                    send([summary_embed(w, pos, equity, "📡 A seguir nova carteira")])
                else:
                    base = prev.setdefault("alerted", dict(prev["pos"]))
                    last = prev.setdefault("last_alert", {})
                    fills = fills_since(addr, prev["ts"] - 5000) if pos != prev["pos"] else []
                    events = diff(w, base, pos, fills, prev["pos"], last)
                    if events:
                        log(short(addr), f"{len(events)} event(s)")
                        send(events)
                state[addr] = {"ts": now_ms, "pos": pos, "equity": equity,
                               "alerted": (prev or {}).get("alerted", dict(pos)),
                               "last_alert": (prev or {}).get("last_alert", {})}
            except Exception:
                log("error on", addr)
                traceback.print_exc()
        save_state(state)
        time.sleep(max(1, poll - (time.time() - t0)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        # python bot.py summary  -> post current positions of every wallet
        load_env()
        for w in load_wallets() or []:
            pos, eq = snapshot(w["address"])
            send([summary_embed(w, pos, eq, "📊 Resumo de posições")])
    else:
        main()
