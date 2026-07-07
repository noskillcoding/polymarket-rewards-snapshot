#!/usr/bin/env python3
"""Scan Polymarket liquidity-reward payouts on-chain into a per-day farmed ledger.

Rewards are pushed once per UTC day as batched ERC-20 transfers from a small set
of **reward treasuries** to each maker. The program has rotated both the token
(USDC.e -> pUSD) and the treasury address over time, so we:

  1. auto-discover the (token, treasury) set from a few long-history seed farmers'
     reward receipts (data-api points us at the exact daily batch txs), merged
     with a seeded known set;
  2. `eth_getLogs` each reward token's Transfers filtered to `from in {treasuries}`
     (both tokens are 6-decimal ~$1 stablecoins, so amounts sum directly in USD);
  3. bucket every transfer by its paid UTC date -> `farmers/daily/<date>.json`
     ({addr: micros}) and fold into `farmers/state/alltime.json`.

Incremental: a committed cursor (last scanned block) means each run scans only new
blocks; the first run backfills the whole program history (payouts go back to ~2024-03,
so it's a ~2-year scan — day-buckets are only built for the last RECENT_DAYS, which
bounds the block-timestamp lookups). On-chain amounts match data-api `usdcSize` /
polyrewards exactly (verified: ImJustKen $1.19M).

Read-only, keyless: uses the same Tenderly public gateway the engine uses (its
free archive tier served 216k-block ranges without a token).

Usage:  .venv/bin/python fetch_farmers.py [--rebuild] [--lookback-days N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE = ROOT / "farmers" / "state"
DAILY = ROOT / "farmers" / "daily"

# Polygon archive RPCs (same as the engine's REWARD_RPC). Tenderly's public
# gateway does keyless archive getLogs; publicnode is a recent-blocks fallback.
RPCS = [
    "https://polygon.gateway.tenderly.co",
    "https://polygon-bor-rpc.publicnode.com",
]

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Reward tokens (both 6-decimal, 1:1 USD) — amounts are already micros.
REWARD_TOKENS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",
}

# Seeded reward treasuries (auto-discovery refreshes/extends this every run).
SEED_TREASURIES = [
    "0xf7cd89be08af4d4d6b1522852ced49fc10169f64",  # USDC.e   Feb–Apr
    "0xc288480574783bd7615170660d71753378159c47",  # USDC.e→pUSD  Mar–Jun
    "0xdd8db71ce3be8d71ff148b2163d64da181a29e8b",  # pUSD     May–Jun
    "0x2c2795ea295d5eb51f9121b728ed2ea4e936a709",  # pUSD     Jul–now
]

# Long-history farmers used to (re)discover treasuries each run. Spread across
# eras (some go back to the 2024 start) so treasury rotations are all sampled.
SEED_FARMERS = [
    "0x9d84ce0306f8551e02efef1680475fc0f1dc1344",  # ImJustKen (since 2024-03)
    "0xc8ab97a9089a9ff7e6ef0688e6e591a066946418",  # ArmageddonRewardsBilly
    "0xa3e22cd32aa9238ef7dbcfb4761e33b9eaa1fdf8",  # pootytherewardfarmer
    "0x204f72f35326db932158cba6adff0b9a1da95e14",  # swisstony
    "0xe9076a87c5ed90ef16e6fe6529c943baeca0cff6",  # suntori
    "0x996ac56c9e72b6ca38a30ae9f6e85152d9afe3cc",  # backback
]

BLOCKS_PER_DAY = 43200          # Polygon ~2.0s/block
CHUNK = 120_000                 # getLogs block-range span
GENESIS_BLOCK = 48_000_000      # ~late 2023; the reward program's first payouts are ~2024-03
RECENT_DAYS = 40                # day-bucket window (only these days need per-day files, for 1d/7d/30d)
UA = {"User-Agent": "Mozilla/5.0", "content-type": "application/json"}


def rpc(method: str, params: list, tries: int = 6):
    """One JSON-RPC call, retried across the RPC pool."""
    last = None
    for attempt in range(tries):
        url = RPCS[attempt % len(RPCS)]
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params}, headers=UA, timeout=90)
            j = r.json()
            if "result" in j and j["result"] is not None:
                return j["result"]
            last = j.get("error", j)
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"rpc {method} failed: {last}")


def api(url: str, tries: int = 4):
    last = None
    for attempt in range(tries):
        try:
            return requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"api {url} failed: {last}")


def topic_addr(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def discover_treasuries(known: set[str]) -> set[str]:
    """Sample seed farmers' reward receipts; add any new (token,treasury) senders.

    The transfer's `from` (topic[1]) is the treasury; `to` is the farmer. Catches
    program rotations without a redeploy."""
    found = set(known)
    for f in SEED_FARMERS:
        try:
            rows = api(f"https://data-api.polymarket.com/activity?user={f}&type=REWARD&limit=500")
        except RuntimeError:
            continue
        if not rows:
            continue
        rows.sort(key=lambda e: e["timestamp"])
        fpad = topic_addr(f)
        step = max(1, len(rows) // 8)
        for i in range(0, len(rows), step):
            tx = rows[i]["transactionHash"]
            try:
                rec = rpc("eth_getTransactionReceipt", [tx])
            except RuntimeError:
                continue
            for lg in rec.get("logs", []):
                t = lg["topics"]
                if (t[0].lower() == TRANSFER and len(t) == 3 and t[2].lower() == fpad
                        and lg["address"].lower() in REWARD_TOKENS):
                    found.add("0x" + t[1][-40:].lower())
                    break
            time.sleep(0.2)
    return found


def block_times(blocks: set[int], cache: dict[str, int]) -> dict[int, int]:
    """UTC timestamp per block number (cached across runs, fetched concurrently)."""
    todo = sorted(b for b in blocks if str(b) not in cache)
    print(f"  block-times: {len(todo)} new blocks", flush=True)

    def one(b: int) -> tuple[int, int]:
        return b, int(rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)

    done = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for b, ts in pool.map(one, todo):
            cache[str(b)] = ts
            done += 1
            if done % 400 == 0:
                print(f"  block-times {done}/{len(todo)}", flush=True)
    return {b: cache[str(b)] for b in blocks}


def get_logs(token: str, treasury_topics: list[str], lo: int, hi: int) -> list[dict]:
    """getLogs for `token` Transfers from any treasury, splitting on range errors."""
    try:
        return rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                    "address": token,
                                    "topics": [TRANSFER, treasury_topics]}])
    except RuntimeError:
        if hi - lo < 4000:
            raise
        mid = (lo + hi) // 2
        return get_logs(token, treasury_topics, lo, mid) + get_logs(token, treasury_topics, mid + 1, hi)


def scan(lo: int, hi: int, treasuries: set[str], recent_from: int, exclude: set[str]):
    """Stream reward-token Transfers over (lo, hi].

    The program spans ~2 years, so we stream: every transfer folds into
    `alltime[addr]` immediately (no giant list), and only transfers in the last
    RECENT_DAYS (block >= recent_from) are kept as `(block, addr, micros)` for
    per-day bucketing (windows). -> (alltime_delta, recent)."""
    treasury_topics = [topic_addr(t) for t in sorted(treasuries)]
    alltime_delta: dict[str, int] = defaultdict(int)
    recent: list[tuple[int, str, int]] = []
    for token in REWARD_TOKENS:
        b = lo + 1
        while b <= hi:
            top = min(b + CHUNK - 1, hi)
            logs = get_logs(token, treasury_topics, b, top)
            for lg in logs:
                addr = "0x" + lg["topics"][2][-40:].lower()
                if addr in exclude:
                    continue
                micros = int(lg["data"], 16)
                alltime_delta[addr] += micros
                blk = int(lg["blockNumber"], 16)
                if blk >= recent_from:
                    recent.append((blk, addr, micros))
            print(f"  {REWARD_TOKENS[token]:7} blk {b}-{top}: +{len(logs)} (recent {len(recent)})", flush=True)
            b = top + 1
    return alltime_delta, recent


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="ignore cursor; rescan from scratch")
    ap.add_argument("--genesis", type=int, default=GENESIS_BLOCK, help="backfill start block")
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    DAILY.mkdir(parents=True, exist_ok=True)
    cursor = load_json(STATE / "cursor.json", {})
    alltime = defaultdict(int, {k: int(v) for k, v in load_json(STATE / "alltime.json", {}).items()})
    btcache = load_json(STATE / "blocktimes.json", {})
    if args.rebuild:
        cursor, alltime, btcache = {}, defaultdict(int), {}
        for f in DAILY.glob("*.json"):
            f.unlink()

    head = int(rpc("eth_blockNumber", []), 16)
    treasuries = discover_treasuries(set(load_json(STATE / "treasuries.json", SEED_TREASURIES)))
    print(f"head={head}  treasuries={len(treasuries)}")

    # Skip treasury mechanics: transfers to the reward tokens, treasuries, or zero
    # (not payouts). Remaining system contracts are dropped at rank time (no lb-api
    # user record) in compute_farmers.py.
    exclude = {t.lower() for t in REWARD_TOKENS} | {t.lower() for t in treasuries} \
        | {"0x0000000000000000000000000000000000000000"}

    lo = cursor.get("last_block", args.genesis)   # first run: full history from genesis
    if lo >= head:
        print("nothing new to scan")
        return 0
    recent_from = head - RECENT_DAYS * BLOCKS_PER_DAY
    print(f"scanning ({lo}, {head}]  (~{(head - lo) / BLOCKS_PER_DAY:.0f} days); "
          f"day-bucketing blocks >= {recent_from}")

    alltime_delta, recent = scan(lo, head, treasuries, recent_from, exclude)
    for a, m in alltime_delta.items():
        alltime[a] += m

    # per-day files only for recent transfers (windows). block timestamps fetched
    # only for those blocks -> bounded even across the full backfill.
    times = block_times({b for b, _, _ in recent}, btcache)
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for block, addr, micros in recent:
        day = datetime.fromtimestamp(times[block], timezone.utc).strftime("%Y-%m-%d")
        per_day[day][addr] += micros
    for day, adds in per_day.items():
        f = DAILY / f"{day}.json"
        cur = defaultdict(int, {k: int(v) for k, v in load_json(f, {}).items()})
        for a, m in adds.items():
            cur[a] += m
        f.write_text(json.dumps(cur, separators=(",", ":")))

    # prune day files older than the window we serve (keep a small margin)
    if per_day:
        latest = datetime.strptime(max(per_day), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for f in DAILY.glob("*.json"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (latest - d).days > RECENT_DAYS + 3:
                f.unlink()

    (STATE / "alltime.json").write_text(json.dumps(alltime, separators=(",", ":")))
    (STATE / "blocktimes.json").write_text(json.dumps(btcache, separators=(",", ":")))
    (STATE / "treasuries.json").write_text(json.dumps(sorted(treasuries), indent=0))
    (STATE / "cursor.json").write_text(json.dumps(
        {"last_block": head, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ"),
         "recent_days": sorted(per_day)}, indent=2))

    print(f"\nscanned ({lo},{head}]; {sum(len(v) for v in per_day.values())} recent day-rows over "
          f"{len(per_day)} days; {len(alltime)} lifetime farmers; ${sum(alltime.values())/1e6:,.0f} all-time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
