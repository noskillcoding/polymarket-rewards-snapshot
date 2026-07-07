#!/usr/bin/env python3
"""Build the farmer-leaderboard page data from the on-chain farmed ledger.

Reads `farmers/daily/<date>.json` + `farmers/state/alltime.json` (written by
fetch_farmers.py), ranks a **separate top-N leaderboard per window** (1d/7d/30d/
all, each by farmed in that window, farmed >= $10 floor), enriches each ranked
farmer with windowed **volume** + display name from the Polymarket leaderboard
API, and writes `page/farmers-data.js` -> `window.FARMERS`.

`1d/7d/30d` sum the N most-recent *paid* days (payouts land ~00:00 UTC daily);
`all` = the cumulative ledger. Farmed is on-chain USD (USDC.e/pUSD, 6-dec).

Volume = sum of trade `size` (shares), Polymarket's own "Volume" metric. Windowed
volume (1d/7d/30d) is summed from the **data-api activity feed** bucketed by UTC
calendar day — lb-api's windowed endpoint often returns an empty row for real traders
(showing them as $0), while data-api can't. Crucially, each volume window is aligned
to the exact days the farmed window rewards: a payout on day D pays for liquidity
EARNED on day D-1, so farmed-bucket D is paired with volume traded on day D-1 (not a
runtime-relative trailing window). data-api's ~5k-fill page cap is floored from lb-api
(no cap); both only undercount, so we take the max. All-time volume is lb-api's.
(Verified: lb-api `amount` == data-api sum(size) to the cent.)

Usage:  .venv/bin/python compute_farmers.py
Env:    MAX_ROWS (default 3000), FLOOR_USD (default 10), VOL_WORKERS (default 12),
        SANITY_MIN_FARMERS (default 500)
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE = ROOT / "farmers" / "state"
DAILY = ROOT / "farmers" / "daily"

WINDOWS = ["1d", "7d", "30d", "all"]
WIN_DAYS = {"1d": 1, "7d": 7, "30d": 30}
MAX_ROWS = int(os.environ.get("MAX_ROWS", "3000"))
FLOOR = float(os.environ.get("FLOOR_USD", "10")) * 1e6          # micros
WORKERS = int(os.environ.get("VOL_WORKERS", "12"))
UA = {"User-Agent": "Mozilla/5.0"}

LBAPI = "https://lb-api.polymarket.com/volume"
DATAAPI = "https://data-api.polymarket.com/activity"
DA_PAGE = int(os.environ.get("DA_PAGE", "500"))
DA_MAX_OFFSET = int(os.environ.get("DA_MAX_OFFSET", "4500"))   # data-api 400s past ~5000 fills


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


# Non-farmer recipients of treasury transfers (the reward token contracts, the
# treasuries themselves, the zero address) — treasury mechanics, not payouts.
# Any remaining system contract is caught at rank time: it has no lb-api user
# record (null volume), whereas a real efficient farmer has small-but-nonzero
# volume. See docs/farmer-leaderboard.md §7.
REWARD_TOKENS = {"0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                 "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"}
ZERO = "0x0000000000000000000000000000000000000000"

# Known non-farmer accounts: real recipients of official-treasury reward transfers
# that are NOT part of the daily-liquidity-farming population this board ranks.
# `0x9133…c00b` is paid HOURLY (401 payouts spread across all 24h, ~$86.8k) with
# ZERO trades / positions / value / username — a bot/programmatic liquidity account
# on a special hourly-reward track, not a retail daily farmer. Its $86k/~$0-volume
# row also produced a nonsense ~312,000% ratio. Excluded 2026-07-08 (operator call);
# reversible — delete the entry to restore. If more hourly-cadence accounts appear,
# generalize to a reward-transfers-per-day filter in fetch_farmers.
EXCLUDED_ACCOUNTS = {
    "0x913313ea6afc9b524d04eafdfaebced79bb0c00b",
}


def hard_exclude() -> set[str]:
    treas = {t.lower() for t in load(STATE / "treasuries.json", [])}
    return REWARD_TOKENS | treas | EXCLUDED_ACCOUNTS | {ZERO}


def _get(url: str):
    """GET JSON with retries. Returns parsed JSON, the string ``"CAP"`` on a 400
    (data-api's pagination limit), or ``None`` on persistent failure."""
    for attempt in range(5):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 400:
                return "CAP"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5 * (attempt + 1))
    return None


def lb_volume(addr: str, window: str) -> tuple[float | None, str | None]:
    """lb-api windowed volume (sum of trade shares) + display name. Returns
    ``(amount, name)``; ``(0.0, None)`` when lb-api returns no row for this
    window (often spurious for short windows — patched from data-api); and
    ``(None, None)`` on a hard error (used as the system-contract signal)."""
    r = _get(f"{LBAPI}?window={window}&address={addr}")
    if isinstance(r, list):
        if r:
            return float(r[0]["amount"]), (r[0].get("name") or r[0].get("pseudonym") or None)
        return 0.0, None
    return None, None


def dataapi_days(addr: str, earliest: str) -> tuple[dict[str, float], bool, str | None, int]:
    """Sum trade ``size`` (shares — the same metric lb-api reports, verified to
    the cent) per **UTC calendar day** from the data-api activity feed, paging
    newest-first. Returns ``(day_sums, capped, name, n_trades)`` where
    ``day_sums`` maps ``"YYYY-MM-DD" -> shares`` and ``capped`` means paging
    stopped at the offset limit before reaching ``earliest`` (oldest day needed),
    so the oldest days may undercount. Calendar days let us align each volume
    window to the exact days the farmed window rewards (payout day D pays for
    earned day D-1), instead of a runtime-relative trailing window."""
    day_sums: dict[str, float] = {}
    name = None
    off = nt = 0
    capped = False
    while True:
        b = _get(f"{DATAAPI}?user={addr}&type=TRADE&limit={DA_PAGE}&offset={off}")
        if b == "CAP":
            capped = True
            break
        if not b:
            break
        oldest_day = None
        for x in b:
            ts = int(x.get("timestamp") or 0)
            sz = float(x.get("size") or 0)
            day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            day_sums[day] = day_sums.get(day, 0.0) + sz
            if name is None:
                name = x.get("name") or x.get("pseudonym") or None
            oldest_day = day
        nt += len(b)
        off += len(b)
        if len(b) < DA_PAGE:
            break
        if oldest_day is not None and oldest_day < earliest:
            break                          # past the oldest needed day (string cmp is chronological)
        if off > DA_MAX_OFFSET:
            capped = True
            break
    return day_sums, capped, name, nt


def main() -> int:
    excl = hard_exclude()
    alltime = {k: int(v) for k, v in load(STATE / "alltime.json", {}).items() if k not in excl}
    if not alltime:
        print("no ledger — run fetch_farmers.py first", file=sys.stderr)
        return 1
    cursor = load(STATE / "cursor.json", {})
    days = sorted((p.stem for p in DAILY.glob("*.json")), reverse=True)  # newest first

    def window_sum(win: str) -> dict[str, int]:
        if win == "all":
            return alltime
        acc: dict[str, int] = {}
        for d in days[:WIN_DAYS[win]]:
            for a, m in load(DAILY / f"{d}.json", {}).items():
                if a not in excl:
                    acc[a] = acc.get(a, 0) + int(m)
        return acc

    # rank each window independently; collect the (addr, window) volume jobs
    ranked: dict[str, list[tuple[str, int]]] = {}
    for win in WINDOWS:
        rows = [(a, m) for a, m in window_sum(win).items() if m >= FLOOR]
        rows.sort(key=lambda kv: -kv[1])
        ranked[win] = rows[:MAX_ROWS]
        print(f"  {win:3}: {len(rows)} farmers >= ${FLOOR/1e6:.0f}  -> top {len(ranked[win])}")

    # ---- volume enrichment --------------------------------------------------
    # Volume = sum of trade `size` (shares) — Polymarket's own "Volume" metric
    # (verified: lb-api `amount` == data-api activity sum(size) to the cent).
    # WINDOWED volume comes from the data-api activity feed bucketed by UTC
    # calendar day: lb-api's *windowed* endpoint frequently returns an EMPTY row
    # for real traders (7d empty while 30d/all populate), which had shown active
    # farmers as $0 — the activity feed can't do that. We align each volume window
    # to the exact days the farmed window rewards: a payout on UTC day D pays for
    # liquidity provided on the EARNED day D-1 (domain doc, Stream B), so a farmed
    # daily bucket dated D is paired with volume traded on day D-1. That makes
    # "1d" = farmed-for-day-X vs volume-traded-on-day-X, NOT a runtime-relative
    # trailing 24h. data-api stops at a ~5k-fill page cap; for a capped farmer we
    # floor the (few) truncated old days with lb-api's trailing windows and take
    # the max — both sources only undercount. All-time volume is lb-api's
    # (complete for whales), max'd against the data-api partial. Drop a candidate
    # (system contract) only if it has no positive all-time volume AND no trades.
    win_cands = {a for win in ("1d", "7d", "30d") for a, _ in ranked[win]}
    all_cands = {a for a, _ in ranked["all"]} | win_cands

    def earned_day(payout_date: str) -> str:            # payout day D pays for earned day D-1
        return (datetime.strptime(payout_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                - timedelta(days=1)).strftime("%Y-%m-%d")

    # the set of earned (traded) days each farmed window covers
    earned = {win: sorted({earned_day(d) for d in days[:WIN_DAYS[win]]}) for win in ("1d", "7d", "30d")}
    earliest = min((d for ds in earned.values() for d in ds), default="9999-12-31")

    def enrich(addr: str) -> tuple[str, dict]:
        lb_all, lb_name = lb_volume(addr, "all")
        rec = {"name": lb_name, "lb_all": lb_all, "nt": 0, "days": {}, "capped": False, "lb_win": {}}
        if addr in win_cands:
            day_sums, capped, da_name, nt = dataapi_days(addr, earliest)
            rec["nt"] = nt
            rec["days"] = day_sums
            rec["capped"] = capped
            if not rec["name"]:
                rec["name"] = da_name
            if capped:                              # recover truncated old days from lb-api trailing
                for w in ("1d", "7d", "30d"):
                    rec["lb_win"][w] = lb_volume(addr, w)[0]
        return addr, rec

    # Optional dev cache (env VOL_CACHE_FILE): reuse fetched recs so a filter-only
    # re-run skips the ~45-min fetch. The cron leaves it unset -> always fresh.
    cache_path = Path(os.environ["VOL_CACHE_FILE"]) if os.environ.get("VOL_CACHE_FILE") else None
    recs: dict[str, dict] = ({a: r for a, r in load(cache_path, {}).items() if a in all_cands}
                             if cache_path else {})
    todo = [a for a in all_cands if a not in recs]
    print(f"enriching {len(all_cands)} farmers ({len(win_cands)} windowed)"
          + (f"; reused {len(recs)} cached, fetching {len(todo)}, {WORKERS} workers..."
             if recs else f", {WORKERS} workers..."))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(enrich, a) for a in todo]
        for i, fut in enumerate(futs):
            a, rec = fut.result()
            recs[a] = rec
            if (i + 1) % 500 == 0:
                print(f"  enriched {i + 1}/{len(todo)}")
    if cache_path:
        cache_path.write_text(json.dumps(recs))
        print(f"  wrote vol cache {cache_path} ({len(recs)} recs)")

    def is_user(a: str) -> bool:      # positive all-time volume OR any trades
        r = recs[a]
        return (r.get("lb_all") or 0) > 0 or r.get("nt", 0) > 0

    def window_vol(a: str, win: str) -> float:
        r = recs[a]
        if win == "all":
            return max(r.get("lb_all") or 0.0, sum(r.get("days", {}).values()))
        v = sum(r.get("days", {}).get(d, 0.0) for d in earned[win])
        if r.get("capped"):           # capped whale: floor truncated days with lb-api trailing
            v = max(v, r.get("lb_win", {}).get(win) or 0.0)
        return v

    def display_name(a: str) -> str:
        n = recs[a].get("name")
        short = a[:6] + "…" + a[-4:]
        if not n or n.startswith("0x") or len(n) > 24:
            return short
        return n

    out_windows = {}
    dropped = 0
    for win in WINDOWS:
        rows = []
        for a, m in ranked[win]:
            if not is_user(a):      # no all-time volume and no trades -> system contract
                dropped += 1
                continue
            v = window_vol(a, win)
            rows.append({"a": a, "n": display_name(a),
                         "f": round(m / 1e6, 2), "v": round(v, 2)})
        out_windows[win] = rows
    print(f"dropped {dropped} non-user (no all-time volume, no trades) rows across windows")

    # drop identified system contracts (no all-time volume, no trades) from the headline too
    non_users = {a for a in all_cands if not is_user(a)}
    clean = {a: m for a, m in alltime.items() if a not in non_users}
    all_farmers = len(clean)
    if len(out_windows["all"]) < int(os.environ.get("SANITY_MIN_FARMERS", "500")):
        print(f"SANITY FAIL: only {len(out_windows['all'])} all-time farmers — refusing to write",
              file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    meta = {
        "ts": now.strftime("%Y-%m-%dT%H-%M-%SZ"),
        "label": now.strftime("%-d %b %H:%M UTC"),
        "prev_day": earned_day(days[0]) if days else None,   # the earned/traded day "1d" refers to
        "windows": WINDOWS,
        "counts": {w: len(out_windows[w]) for w in WINDOWS},
        "floor_usd": FLOOR / 1e6,
        "lifetime_farmers": all_farmers,
        "alltime_paid_usd": round(sum(clean.values()) / 1e6),
        "scanned_days": len(days),
        "ledger_updated": cursor.get("updated"),
    }
    dest = ROOT / "page" / "farmers-data.js"
    dest.write_text("window.FARMERS = " + json.dumps({"meta": meta, "windows": out_windows},
                                                     ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"\nwrote {dest} ({dest.stat().st_size/1e6:.2f} MB)")
    for w in WINDOWS:
        top = out_windows[w][0] if out_windows[w] else None
        print(f"  {w:3}: {len(out_windows[w])} rows"
              + (f"; #1 {top['n']} farmed ${top['f']:,.0f} vol ${top['v']:,.0f}"
                 if top and top['v'] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
