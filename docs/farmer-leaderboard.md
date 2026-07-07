# Farmer Leaderboard — spec / build notes

A **second page** in the analytics repo (`page/farmers.html`): the top Polymarket
**liquidity farmers** by **rewards received** vs **volume traded** — i.e. how
efficiently each farms, and by proxy **how often they get filled**. Low
farmed/volume ⇒ churned / picked off; high ⇒ resting quietly, rarely filled;
**∞** ⇒ earned rewards but never filled that window (peak efficiency).

Independent of the market-snapshot page; shares its repo, theme, and branch-push
Pages deploy. No secrets — all public channels; keyless RPC.

---

## 1. What the page shows

**Columns:** Trader (name → `polymarket.com/profile/<address>`) · Farmed
($ rewards) · Volume ($ traded) · **Farmed / Volume** (percent; **∞** when volume 0).

**Windows:** **1d · 7d · 30d · all** (no 90d — lb-api volume doesn't offer it and
historical daily volume can't be backfilled). **1d = the last completed EARNED day**
(the traded day whose liquidity a payout rewards). Rewards for UTC day `D` are paid at
`D+1` 00:00, so a farmed bucket dated `D` = earned day `D-1`; volume for each window is
summed over those exact earned days (aligned, not a runtime-relative trailing window),
so Farmed and Volume in every row cover the identical period.

**Per-window leaderboards:** each window is its own top **3000**, ranked by farmed
in that window, farmed **≥ $10**. Default = all-time. 3000 is a **cap, not a floor** —
after the $10 gate a short window may have fewer (1d ≈ 1k in practice).

---

## 2. Data sources (all public, no Dune, no API key)

| Field | Source |
|---|---|
| **Farmed** + **farmer discovery** + all windows | on-chain reward-token Transfers **from** the reward treasuries, via the keyless **Tenderly gateway** `https://polygon.gateway.tenderly.co` |
| **Volume — all-time** + **trader name** | `lb-api.polymarket.com/volume?window=all&address=Y` (browser UA required; name/pseudonym in the response) |
| **Volume — 1d/7d/30d** | sum of trade `size` from `data-api.polymarket.com/activity?user=Y&type=TRADE`, self-windowed by timestamp |
| **Profile link** | `polymarket.com/profile/<address>` |

**⚠️ Volume = sum of trade `size` (shares), NOT `usdcSize`.** That is Polymarket's own
"Volume" metric — verified: `lb-api amount == data-api Σsize` to the cent (`usdcSize` =
size×price is ~2–2.5× smaller, the wrong field). **lb-api's *windowed* endpoint is
unreliable**: for many real traders it returns an empty row for 1d/7d (and even 30d) while
`all`/`30d` are populated — e.g. a farmer who traded 5 days ago gets `[]` for 7d. Taken at
face value that showed active farmers as **$0 volume** (inflating their ratio to look
"never filled"). Fix: compute 1d/7d/30d from the **data-api activity feed** (self-windowed,
can't do that), and keep lb-api for `all` (complete for whales — data-api paging caps at
~5k fills). Both sources only ever *undercount* (lb→empty, data→page-cap), so per window we
take `max(lb, data-api)`; when data-api is truncated we patch the window from lb-api.

**The reward program rotates token AND treasury (~monthly)** — verified on-chain,
amounts match data-api `usdcSize` exactly:
- Tokens (both 6-dec, 1:1 USD → sum in USD): **USDC.e** `0x2791bca1…84174` (Feb–Apr),
  **pUSD** `0xc011a7e1…82dfb` (May–now).
- Treasuries seen: `0xf7cd89…`, `0xc288…`, `0xdd8db7…`, `0x2c2795…` — **auto-discovered**
  from seed farmers' REWARD-tx receipts each run (the Transfer `from` is the treasury;
  the tx `from` is a relayer EOA, not the treasury — don't use it). These 4 cover the whole
  history.
- **⚠️ The program is ~2 years old (first payouts ~2024-03, block ~54.97M).** Scan from a
  2023 genesis block (`GENESIS_BLOCK = 48M`), NOT a short lookback — a 160-day scan
  undercounts old farmers badly (ImJustKen showed 1.9% of true). To stay cheap over 2 years,
  stream all-time totals and only fetch block timestamps / build per-day files for the last
  `RECENT_DAYS` (=40, for the windows). **data-api `usdcSize` == the on-chain transfer
  exactly**, so full-history on-chain matches polyrewards.fun to the dollar (verified:
  ImJustKen $1.198M, ArmageddonBilly $946k, etc.).

**Probed constraints:** lb-api volume windows are only `1d/7d/30d/all`; its leaderboard
form is capped at 50 rows (`offset` ignored) so it can't enumerate farmers — discovery
must be on-chain. lb-api needs a browser UA (else 403); ~13.5 req/s, no throttle at 40
rapid calls. Rewards have no leaderboard endpoint and no per-market breakdown (data-api
REWARD entries have blank `conditionId`). publicnode's free tier refuses archive getLogs.

---

## 3. Pipeline

```
fetch_farmers.py    incremental on-chain scan -> committed farmed ledger
compute_farmers.py  rank each window + fetch lb-api volumes -> page/farmers-data.js
page/farmers.html + page/farmers.js   the page (reuses index.html theme/chrome)
.github/workflows/farmers.yml         DAILY cron (01:11 UTC), commits ledger + data
```

**fetch_farmers.py** — discover treasuries; `eth_getLogs` each reward token's Transfers
from `{treasuries}` over `(cursor.last_block, head]` (chunked, splits on RPC range error);
resolve each unique block's timestamp (**concurrent** — the sequential version was the
bottleneck; ~11k blocks); bucket by paid UTC date into `farmers/daily/<date>.json`
(`{addr: micros}`) and fold into `farmers/state/alltime.json`; advance the committed cursor.
Skips transfers to the reward tokens / treasuries / zero (treasury mechanics, not payouts).
First run backfills the full ~2-year history (from `GENESIS_BLOCK`); later runs are
incremental. Block timestamps (recent only) cache in `farmers/state/blocktimes.json`.
`--rebuild` rescans from scratch.

**compute_farmers.py** — sum `1d/7d/30d` from the N most-recent daily files, `all` from
`alltime.json`; rank each window (≥ $10, top 3000). Enrich each ranked farmer with volume:
lb-api `all` (all-time + name), and for anyone in a 1d/7d/30d list a data-api activity scan
(page newest-first, bucket trade `size` into 1d/7d/30d, stop past the 30d edge). Windowed
volume = `max(data-api, lb-api-if-page-capped)`; all-time = `max(lb-api, data-api partial)`.
**Filter:** drop a candidate only if it has **no lb-api record AND no trades** (a non-user
system contract); a real user with zero window volume → 0 (never filled → ratio ∞). Write
`page/farmers-data.js`.

**Cost/run:** reward scan = ~25 getLogs + ~11k block-times (concurrent, one-time; tiny
incrementally). Volume = ~5k lb-api `all` calls + a data-api activity scan for the ~4k
windowed candidates (1–9 pages each) — ~15–20 min, 12 workers. Well within the daily job.

---

## 4. Output — `page/farmers-data.js`

```js
window.FARMERS = {
  meta: { ts, label, prev_day,           // prev_day = the UTC day "1d" refers to
          windows:["1d","7d","30d","all"], counts:{...},
          floor_usd:10, lifetime_farmers, alltime_paid_usd, scanned_days, ledger_updated },
  windows: {                              // each pre-ranked by farmed desc
    "1d":  [{ a:address, n:name, f:farmed$, v:volume$ }, ...],
    "7d": [...], "30d": [...], "all": [...]
  }
}
```
Ratio = f/v (client-side; ∞ when v==0). Name falls back to short address when missing,
address-like, or > 24 chars.

---

## 5. Page

Reuses `index.html`'s theme system (CSS vars, dark/light toggle, anti-flash), chrome, and
"Give it to your AI agent" button; cross-linked with the market page (`.navlink`). Window
tabs; sortable table (default farmed desc); ∞-rows sort as most-efficient on the ratio sort;
paginated 100/row. Zero-build vanilla JS.

---

## 6. Deploy

`farmers.yml` (daily) updates the ledger and **commits** `farmers/` + `page/farmers-data.js`;
it does **not** deploy. The hourly `update` workflow force-pushes all of `page/` to `gh-pages`
(regenerating the market `data.js` fresh) and carries the committed `farmers-data.js` live —
one deployer touches gh-pages, and the stale committed market seed is never shipped. Farmer
refresh appears live at the next hourly `update` run (≤ 1h).

---

## 7. Edge cases & decisions

- **∞ ratio** — real user, zero volume in the window = earned but never filled (peak
  efficiency); shown ∞, sorts to top on ratio-desc.
- **System contracts** — dropped when a candidate has no lb-api all-time record **and** no
  data-api trades (§3). `eth_getCode` does NOT work (Polymarket proxy wallets are contracts too).
- **Non-standard reward accounts** — a real treasury reward recipient that isn't a daily-liquidity
  farmer. `0x9133…c00b` (EOA) is paid **hourly** (401 payouts across all 24h, ~$86.8k) with zero
  trades/positions/value/name — a bot/programmatic account on a special hourly-reward track; its
  $86k/~$0-volume row gave a nonsense ~312,000% ratio. Hard-excluded (`EXCLUDED_ACCOUNTS` in
  `compute_farmers.py`), reversible. Normal farmers get one daily lump at 00:00 UTC; if more
  hourly-cadence accounts appear, generalize to a reward-transfers-per-day filter in fetch.
- **Farmed/Volume period alignment** — engineered, not just documented. A payout on UTC
  day `D` rewards liquidity earned on `D-1`, so each farmed window's volume is summed over
  the matching *earned* days (`payout_date − 1`), by exact UTC calendar day — Farmed and
  Volume in a row always cover the same period. (Volume comes from data-api day-buckets;
  the ≤24h trailing offset the old lb-api-windowed approach had is gone.)
- **90d dropped** — lb-api has no 90d and historical daily volume can't be backfilled.
- **Headline totals** exclude the hard set + identified null-lifetime contracts; deep
  (unranked) system contracts may leave a small residue — negligible vs the totals.

## 8. Open items

- git size of the daily-churning `alltime.json` (~6 MB) + `farmers-data.js` — commit vs
  Actions cache (currently commit; simplest, robust). Prune `daily/` > ~35 days.
- Distributor/treasury completeness relies on seed-farmer discovery; a brand-new treasury
  is caught on the next run. Log when a new one appears.
</content>
