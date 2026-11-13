# Hyperliquid whale tracker

Polls positions (main perps + all HIP-3 dexes) of the wallets in `wallets.json`
and posts open / close / flip / size-change / liquidation alerts to a Discord webhook.
Python 3 stdlib only.

- `wallets.json`: wallets + per-wallet thresholds (`min_position_usd`, `min_change_usd`,
  `min_change_pct`, `cooldown_min`, `enabled`). Hot-reloaded every cycle, no restart needed.
- `.env`: `DISCORD_WEBHOOK_URL`, `POLL_SECONDS`.
- `python3 bot.py summary`: post the current positions of every wallet.

## Deploy: GitHub Actions (current)
`.github/workflows/tracker.yml` runs every 5 min; each run polls for ~9 min (`RUN_SECONDS=540`)
and runs are serialized, so coverage is near-continuous. State is carried between runs in the
Actions cache. Webhook comes from the repo secret `DISCORD_WEBHOOK_URL`.
Repo must be public (unlimited Actions minutes).

## Deploy (Ubuntu VPS, alternative)
    scp -r bot.py wallets.json .env deploy ubuntu@IP:~/hl-tracker/
    sudo cp ~/hl-tracker/deploy/hl-tracker.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now hl-tracker
    journalctl -u hl-tracker -f

## Market bottom signals (`signals.py`)
Every 5 min checks BTC: funding (OKX+HL), open interest drop, OKX retail long/short,
Coinbase premium and (optional) US spot ETF flows. Alerts when a signal flips; daily summary at 08:00 UTC.
ETF flows need a free SoSoValue API key in the repo secret `SOSO_API_KEY`.
