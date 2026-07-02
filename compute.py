#!/usr/bin/env python3
"""Join a raw snapshot (fetch_snapshot.py output) into page/data.js.

Per market: category (tag precedence), spread bucket (live book), 24h-volume
bucket (gamma), age / time-to-resolution buckets, competitiveness bucket
(reward §3 scoring vs the live book — see docs/polymarket-liquidity-rewards.md),
and reward $/day. Output: page/data.js  ->  window.SNAPSHOT = {meta, markets}.

Competitiveness (approximation notes): book levels are aggregated, not
individual orders, so "level size >= min_size" stands in for the per-order
eligibility cutoff; the adjusted midpoint discards sub-min_size levels per the
official anti-manipulation rule.
  no farmers -> nothing currently scores (Q_min == 0: empty/one-sided book
                outside [0.10,0.90], no in-band qualifying size, or no book)
  thin       -> a NEW min_size quote joining the best in-band price each side
                would capture >= 25% of the pool (score-based; the naive
                "shares < 2 x min_size" cut is degenerate — any two-sided
                qualifying book has >= 2 x min_size by construction)
  contested  -> everything else

Usage:  .venv/bin/python analytics/compute.py [data/<ts>] [--history DIR]
        (snapshot defaults to the latest data/<ts> dir)

Env:    PERIOD_MIN          displayed update cadence, default 30 (keep in sync
                            with the workflow cron)
        SANITY_MIN_MARKETS  deploy floor, default 1000 markets
        SANITY_MIN_TOTAL    deploy floor, default $10,000/day
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# --- category: first match wins (agreed precedence) --------------------------

CATEGORY_RULES = [
    ("Sports", r"sport|soccer|nfl|nba|mlb|nhl|epl|fifa|world cup|tennis|golf|ufc|mma"
               r"|boxing|cricket|rugby|f1|formula|baseball|basketball|hockey|esport"
               r"|counter.strike|cs2|dota|league of legends|valorant|chess|olympic"
               r"|la liga|serie a|bundesliga|ligue 1|premier league|champions league"
               r"|europa|wnba|ncaa|college football|college basketball|darts|snooker"
               r"|cycling|wrestling|atp|wta|grand slam|wimbledon|pga|nascar|motogp"
               r"|super bowl|stanley cup|playoff|grand prix|racing"),
    ("Crypto", r"crypto|bitcoin|\bbtc\b|ethereum|\beth\b|solana|\bsol\b|\bxrp\b"
               r"|dogecoin|memecoin|defi|\bnft\b|stablecoin|altcoin|hyperliquid"
               r"|coinbase|binance|airdrop|blockchain"),
    ("Politics", r"politic|election|midterm|senate|house|congress|president|trump"
                 r"|geopolitic|ukraine|russia|israel|gaza|iran|nato|\bwar\b|court"
                 r"|scotus|supreme|impeach|governor|mayor|primar|poll|approval"
                 r"|cabinet|white house|legislation|government|shutdown|minister"
                 r"|parliament|tariff|epstein|world leader|global affairs|diplomacy"
                 r"|treaty|sanction|immigration|deport"),
    ("Pop-culture", r"culture|celebrit|entertainment|movie|film|box office|music"
                    r"|\btv\b|television|awards|oscar|grammy|emmy|golden globe"
                    r"|kardashian|taylor swift|mrbeast|youtube|tiktok|twitter|tweet"
                    r"|elon|royal|time person|miss universe|eurovision|reality"
                    r"|love island|bachelor|streaming|\bgta\b|gaming|video game"
                    r"|billboard|spotify|netflix|album|artist|actor"),
    ("Weather", r"weather|temperature|climate|rain|snow|hurricane|storm|heat"),
    ("Economy", r"finance|econom|\bfed\b|fomc|interest rate|inflation|\bcpi\b|\bgdp\b"
                r"|jobs report|unemployment|macro|business|stock|s&p|nasdaq|\bdow\b"
                r"|earnings|\bipo\b|compan|commodit|\boil\b|gold|silver|treasury"
                r"|recession|housing|real estate|mention|openai|\bai\b|tech"),
]
CATEGORY_RES = [(name, re.compile(pat)) for name, pat in CATEGORY_RULES]


def categorize(tags: list[str]) -> str:
    joined = " | ".join(t.lower() for t in tags)
    for name, rx in CATEGORY_RES:
        if rx.search(joined):
            return name
    return "Other"


# --- bucketing ---------------------------------------------------------------

def spread_bucket(cents: float | None) -> str:
    if cents is None:
        return "no book"
    for hi, lbl in [(5, "0–5¢"), (10, "5–10¢"), (20, "10–20¢"), (30, "20–30¢"), (50, "30–50¢")]:
        if cents <= hi:
            return lbl
    return ">50¢"


def mid_bucket(mp: float | None) -> str:
    if mp is None:
        return "no book"
    for hi, lbl in [(0.10, "<10¢"), (0.30, "10–30¢"), (0.50, "30–50¢"),
                    (0.70, "50–70¢"), (0.90, "70–90¢")]:
        if mp < hi:
            return lbl
    return ">90¢"


def minshares_bucket(ms: float) -> str:
    if ms <= 0:
        return "none"
    for hi, lbl in [(20, "≤20"), (50, "21–50"), (100, "51–100"), (250, "101–250")]:
        if ms <= hi:
            return lbl
    return ">250"


def reward_bucket(rw: float) -> str:
    if rw <= 0:
        return "$0"
    for hi, lbl in [(10, "<$10"), (25, "$10–25"), (50, "$25–50"),
                    (100, "$50–100"), (500, "$100–500")]:
        if rw < hi:
            return lbl
    return ">$500"


def volume_bucket(v: float) -> str:
    if v <= 0:
        return "$0"
    for hi, lbl in [(1_000, "<1k"), (10_000, "1–10k"), (100_000, "10–100k")]:
        if v < hi:
            return lbl
    return ">100k"


def days_bucket(days: float | None, none_lbl: str) -> str:
    if days is None:
        return none_lbl
    for hi, lbl in [(1, "<1d"), (7, "1–7d"), (30, "7–30d")]:
        if days < hi:
            return lbl
    return ">30d"


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- competitiveness (§3 scoring vs live book) --------------------------------

def competitiveness(book: dict | None, min_size: float, max_spread_cents: float) -> str:
    if not book or max_spread_cents <= 0:
        return "no farmers"
    bids = [(float(l["price"]), float(l["size"])) for l in book.get("bids") or []]
    asks = [(float(l["price"]), float(l["size"])) for l in book.get("asks") or []]
    if not bids or not asks:
        return "no farmers"  # one-sided touch: no adjusted mid, treat as unclaimed

    # size-cutoff-adjusted midpoint (fall back to raw touch mid if one side
    # has no qualifying level — scoring may still be nonzero via the c-penalty)
    qb = [p for p, s in bids if s >= min_size]
    qa = [p for p, s in asks if s >= min_size]
    if qb and qa:
        mid = (max(qb) + min(qa)) / 2
    else:
        mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2

    v = max_spread_cents

    def side(levels: list[tuple[float, float]], is_bid: bool) -> tuple[float, float]:
        q, best_sc = 0.0, None  # best_sc: tightest in-band qualifying spread (cents)
        for p, s in levels:
            if s < min_size:
                continue
            sc = (mid - p if is_bid else p - mid) * 100  # spread from mid, cents
            if sc < 0 or sc > v:
                continue
            q += ((v - sc) / v) ** 2 * s
            best_sc = sc if best_sc is None else min(best_sc, sc)
        return q, best_sc

    def combine(qb: float, qa: float) -> float:
        if 0.10 <= mid <= 0.90:
            return max(min(qb, qa), max(qb, qa) / 3.0)
        return min(qb, qa)

    q_bid, best_bid_sc = side(bids, True)
    q_ask, best_ask_sc = side(asks, False)
    q_exist = combine(q_bid, q_ask)
    if q_exist <= 1e-9:
        return "no farmers"

    # hypothetical newcomer: min_size per side, joining the best in-band
    # qualifying price (or posting mid-band where a side has none)
    def new_q(best_sc: float | None) -> float:
        sc = best_sc if best_sc is not None else v / 2
        return ((v - sc) / v) ** 2 * min_size

    q_new = combine(new_q(best_bid_sc), new_q(best_ask_sc))
    share = q_new / (q_new + q_exist)
    return "thin" if share >= 0.25 else "contested"


# --- history (long-term time series of tiny aggregates) -----------------------

DIM_KEYS = ["c", "sb", "mpb", "vb", "rwb", "msb", "ab", "rb", "cb"]


def write_history(hist_dir: Path, meta: dict, markets: list[dict]) -> None:
    """Append one ~5 KB line per run to <day>.ndjsonl: totals + all 9 bucket
    distributions (reward-$ sum + market count per bucket). Idempotent: a
    re-run over the same snapshot ts is skipped, not duplicated."""
    hist_dir.mkdir(parents=True, exist_ok=True)
    day_file = hist_dir / f"{meta['ts'][:10]}.ndjsonl"
    if day_file.exists() and f'"{meta["ts"]}"' in day_file.read_text():
        print(f"history: {meta['ts']} already recorded, skipping")
        return
    dims: dict = {}
    for key in DIM_KEYS:
        agg: dict = {}
        for m in markets:
            e = agg.setdefault(m[key], [0.0, 0])
            e[0] += m["rw"]
            e[1] += 1
        dims[key] = {b: [round(v[0]), v[1]] for b, v in agg.items()}
    line = {"ts": meta["ts"], "total": meta["total"], "count": meta["count"], "dims": dims}
    with open(day_file, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"history: appended {meta['ts']} -> {day_file}")


# --- main ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", nargs="?", help="data/<ts> dir (default: latest)")
    ap.add_argument("--history", metavar="DIR",
                    help="append per-run aggregates to DIR/<day>.ndjsonl")
    args = ap.parse_args()

    if args.snapshot:
        p = Path(args.snapshot)
        snap_dir = p if p.is_absolute() else ROOT / p
    else:  # latest timestamped dir (ignore stray files like .DS_Store)
        snap_dir = max(d for d in (ROOT / "data").iterdir() if d.is_dir())
    print(f"snapshot: {snap_dir}")

    sampling = json.loads((snap_dir / "sampling_markets.json").read_text())
    gamma = {r["conditionId"]: r for r in json.loads((snap_dir / "gamma_markets.json").read_text())}
    books = {b["asset_id"]: b for b in json.loads((snap_dir / "books.json").read_text())}
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    snap_ts = datetime.strptime(manifest["ts_utc"], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)

    def yes_token(row: dict) -> str:
        toks = row.get("tokens") or []
        for t in toks:
            if str(t.get("outcome", "")).strip().lower() == "yes" and t.get("token_id"):
                return str(t["token_id"])
        for t in toks:
            if t.get("token_id"):
                return str(t["token_id"])
        return ""

    markets = []
    for r in sampling:
        cid = r.get("condition_id")
        if not cid:
            continue
        g = gamma.get(cid, {})
        rw = sum(float(x.get("rewards_daily_rate") or 0)
                 for x in (r.get("rewards") or {}).get("rates") or [])
        min_size = float((r.get("rewards") or {}).get("min_size") or 0)
        max_spread = float((r.get("rewards") or {}).get("max_spread") or 0)  # CENTS

        book = books.get(yes_token(r))
        spread_c = mid = None
        if book and (book.get("bids") or None) and (book.get("asks") or None):
            bb = max(float(l["price"]) for l in book["bids"])
            ba = min(float(l["price"]) for l in book["asks"])
            spread_c = round((ba - bb) * 100, 1)
            mid = round((bb + ba) / 2, 3)

        vol = g.get("volume24hr")
        vol = float(vol) if vol is not None else 0.0

        start = parse_iso(g.get("startDate"))
        end = parse_iso(g.get("endDate")) or parse_iso(r.get("end_date_iso"))
        age_d = (snap_ts - start).total_seconds() / 86400 if start else None
        end_d = (end - snap_ts).total_seconds() / 86400 if end else None
        if end_d is not None and end_d < 0:
            end_d = 0.0  # past end date but still live -> "<1d"

        markets.append({
            "q": r.get("question") or g.get("question") or cid[:16],
            "slug": r.get("market_slug") or g.get("slug") or "",
            "c": categorize(r.get("tags") or []),
            "sb": spread_bucket(spread_c),
            "sp": spread_c,
            "mp": mid,
            "mpb": mid_bucket(mid),
            "ms": round(min_size),
            "msb": minshares_bucket(min_size),
            "rwb": reward_bucket(rw),
            "vb": volume_bucket(vol),
            "vn": round(vol),
            "ab": days_bucket(age_d, "unknown"),
            "rb": days_bucket(end_d, "no end"),
            "cb": competitiveness(book, min_size, max_spread),
            "rw": round(rw, 2),
        })

    total = round(sum(m["rw"] for m in markets))

    # sanity gates: a broken venue response (silent pagination truncation,
    # half-outage) must fail the run so the previous deploy keeps serving
    min_markets = int(os.environ.get("SANITY_MIN_MARKETS", "1000"))
    min_total = float(os.environ.get("SANITY_MIN_TOTAL", "10000"))
    if len(markets) < min_markets or total < min_total:
        print(f"SANITY FAIL: {len(markets)} markets / ${total:,}/day is below the "
              f"floors ({min_markets} markets / ${min_total:,.0f}/day) — "
              f"refusing to write data.js", file=sys.stderr)
        return 1

    label = snap_ts.strftime("%-d %b %H:%M UTC")
    out = {
        "meta": {"ts": manifest["ts_utc"], "label": label,
                 "total": total, "count": len(markets),
                 "period_min": int(os.environ.get("PERIOD_MIN", "30"))},
        "markets": markets,
    }
    dest = ROOT / "page" / "data.js"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("window.SNAPSHOT = " + json.dumps(out, ensure_ascii=False) + ";\n")
    print(f"wrote {dest} ({dest.stat().st_size/1e6:.1f} MB): "
          f"{len(markets)} markets, ${total:,}/day\n")

    if args.history:
        hp = Path(args.history)
        write_history(hp if hp.is_absolute() else ROOT / hp, out["meta"], markets)

    # verification: $-weighted distribution per dimension
    for key, name in [("c", "category"), ("sb", "spread"), ("mpb", "mid price"),
                      ("vb", "volume24h"), ("rwb", "reward/day"), ("msb", "min shares"),
                      ("ab", "age"), ("rb", "ends"), ("cb", "competitiveness")]:
        by = defaultdict(float)
        cnt = Counter()
        for m in markets:
            by[m[key]] += m["rw"]
            cnt[m[key]] += 1
        print(f"  {name}:")
        for b, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"    {b:12} ${v:>10,.0f}/day  {v/total*100:5.1f}%   {cnt[b]:5} mkts")

    # what's hiding in Other — top tags, to tune CATEGORY_RULES
    other_tags = Counter(t for r in sampling
                         if categorize(r.get("tags") or []) == "Other"
                         for t in r.get("tags") or [])
    print("\n  top tags in Other:", other_tags.most_common(12))
    return 0


if __name__ == "__main__":
    sys.exit(main())
