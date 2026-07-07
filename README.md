# Polymarket Liquidity Rewards — Market Snapshot

Static dashboard + GitHub Actions pipeline: every hour, at an off-peak minute, it snapshots
**every rewarded Polymarket market** (~8k) and shows where the total daily
liquidity-reward pool ($/day) is going — by category, spread, mid price,
volume, reward size, min shares, age, time to resolution, competitiveness, and yield per $100 of liquidity.
All charts cross-filter; the table drills down to individual markets.

## How it works

```
fetch_snapshot.py   CLOB /sampling-markets (the rewards universe) + Gamma
                    /markets (volume/dates) + CLOB POST /books (live books),
                    fetched in parallel (~20-30 s) -> data/<ts>/ (raw, gitignored)
compute.py          join + bucket 10 dimensions + score competitiveness with the
                    official reward formula -> page/data.js;
                    --history appends ~5 KB of aggregates per run -> history/
                    (committed: a long-term time series of the pool's shape)
page/               zero-dependency static dashboard (vanilla JS, no build)
.github/workflows/update.yml   hourly cron: fetch -> compute -> commit history
                    -> deploy page/ to GitHub Pages
```

Fail-safe: fetch errors, <99% coverage, or an implausibly small universe
(`SANITY_MIN_MARKETS`/`SANITY_MIN_TOTAL`) abort the run before deploy — the
previous page keeps serving, its header turns amber (**STALE**) after two
missed cycles, and GitHub emails the repo owner about the failed workflow.

## Farmer leaderboard (second page)

`page/farmers.html` — the top liquidity **farmers** by rewards received vs volume
traded (Farmed / Volume = how often they get filled), in 1d / 7d / 30d / all-time
windows. Built daily by a separate pipeline; see `docs/farmer-leaderboard.md`.

```
fetch_farmers.py    incremental on-chain reward scan (keyless Tenderly getLogs)
                    -> farmers/ ledger (kept in the Actions cache, not committed)
compute_farmers.py  rank each window + lb-api/data-api volumes -> page/farmers-data.js
.github/workflows/farmers.yml   daily cron (01:11 UTC); caches the ledger, commits
                    only page/farmers-data.js. The hourly `update` deploy carries it
                    to gh-pages (<= 1h).
```

Local dev: `.venv/bin/python fetch_farmers.py` (first run backfills the full ~2-year
program history, ~20 min; later runs are incremental) then
`.venv/bin/python compute_farmers.py` (MAX_ROWS/FLOOR_USD/VOL_WORKERS env).

## Setup (one-time)

1. Create the repo on GitHub (**public**, for free Pages) — don't push yet.
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. Push. The push triggers the first workflow run automatically
   (or run it by hand: **Actions** tab → `update` → *Run workflow*).
4. Page appears at `https://<account>.github.io/<repo>/`.

If the first run fails at the deploy step, Pages wasn't enabled yet (step 2) —
enable it and re-run the workflow; nothing else is affected.

No secrets required — everything reads public Polymarket APIs.

## Local dev

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python fetch_snapshot.py     # ~30 s, writes data/<ts>/
.venv/bin/python compute.py            # writes page/data.js
open page/index.html                   # or: python3 -m http.server -d page 8410
```

## Tuning

- `PERIOD_MIN` (default 30) — cadence shown on the page; keep in sync with the
  cron line in `update.yml`.
- `SANITY_MIN_MARKETS` / `SANITY_MIN_TOTAL` (default 1000 / $10,000) — deploy floors.
- Bucket edges, category tag rules, competitiveness thresholds: `compute.py`.
