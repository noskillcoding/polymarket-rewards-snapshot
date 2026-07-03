# Polymarket Liquidity Rewards — Market Snapshot

Static dashboard + GitHub Actions pipeline: every hour, at an off-peak minute, it snapshots
**every rewarded Polymarket market** (~8k) and shows where the total daily
liquidity-reward pool ($/day) is going — by category, spread, mid price,
volume, reward size, min shares, age, time to resolution, and competitiveness.
All charts cross-filter; the table drills down to individual markets.

## How it works

```
fetch_snapshot.py   CLOB /sampling-markets (the rewards universe) + Gamma
                    /markets (volume/dates) + CLOB POST /books (live books),
                    fetched in parallel (~20-30 s) -> data/<ts>/ (raw, gitignored)
compute.py          join + bucket 9 dimensions + score competitiveness with the
                    official reward formula -> page/data.js;
                    --history appends ~5 KB of aggregates per run -> history/
                    (committed: a long-term time series of the pool's shape)
page/               zero-dependency static dashboard (vanilla JS, no build)
.github/workflows/update.yml   cron */30: fetch -> compute -> commit history
                    -> force-push page/ to gh-pages (branch-based Pages;
                    the Actions deploy path hung indefinitely — see workflow)
```

Fail-safe: fetch errors, <99% coverage, or an implausibly small universe
(`SANITY_MIN_MARKETS`/`SANITY_MIN_TOTAL`) abort the run before deploy — the
previous page keeps serving, its header turns amber (**STALE**) after two
missed cycles, and GitHub emails the repo owner about the failed workflow.

## Setup (one-time)

1. Create the repo on GitHub (**public**, for free Pages) and push. The push
   triggers the first workflow run, which creates the `gh-pages` branch
   (or run it by hand: **Actions** tab → `update` → *Run workflow*).
2. Repo **Settings → Pages → Source: Deploy from a branch → `gh-pages` / root**
   (API: `PUT /repos/{owner}/{repo}/pages` with `build_type=legacy`,
   `source={branch: gh-pages, path: /}`).
3. Page appears at `https://<account>.github.io/<repo>/`.

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
