# TT Energy Strip Dashboard

An evergreen dashboard showing **WTI crude oil** and **Henry Hub natural gas** prices —
18 months of spot history plus forward curve snapshots (today, yesterday, 7 days ago,
1 month ago, 3 months ago, 1 year ago) — for an always-on hallway TV.

## How it runs (serverless)

- **`update_data.py`** fetches spot history (EIA API v2) and the futures strip
  (Yahoo Finance / NYMEX) and writes `energy_data.json`, accumulating
  `curve_snapshots.json` over time.
- A **scheduled GitHub Action** (`.github/workflows/update.yml`) runs the script every
  weekday after market settlement and commits the refreshed data back to the repo.
- **`dashboard.html`** is served by **GitHub Pages**, reads the JSON, renders with
  Chart.js, and self-reloads hourly. Point the TV browser at the Pages URL.

No always-on PC required.

## Editing the dashboard

Edit `dashboard.html` and push (or use GitHub's web editor). Pages redeploys within
~a minute; the TV picks it up on its next hourly reload.

## EIA API key

The key is **not** stored in the code. The Action reads it from the `EIA_KEY`
repository secret. For a local manual run, put the key in `eia_key.txt` (git-ignored)
and run `python update_data.py`.

## Data sources

- Spot prices: EIA API v2 (free)
- Futures strip: Yahoo Finance via the `yfinance` library
