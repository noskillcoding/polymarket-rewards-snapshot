#!/usr/bin/env python3
"""Fetch a near-simultaneous snapshot of the Polymarket rewards universe.

Read-only market-data fetch (geo-independent, no keys, no engine contact).

Three sources, joined on condition_id:
  1. CLOB GET /sampling-markets  (cursor-paginated) — THE rewards universe:
     rewards config (min_size, max_spread in CENTS, rates[].rewards_daily_rate),
     tags, tokens, liveness flags, end_date_iso.
  2. Gamma GET /markets?condition_ids=... (repeated param, chunked 50/req) —
     category, startDate/endDate, volume24hr, volume.
  3. CLOB POST /books (batch <=500 token_ids/req) — live order books, one
     (YES-preferred) token per market: binary rewarded markets have a unified
     symmetric book, so one side carries full depth.

Phase 1 is sequential (cursor); phases 2+3 run fully parallel so the whole
snapshot lands inside a tight window. Raw responses are saved untouched under
analytics/data/<UTC-ts>/ plus a manifest with timings/coverage; all analysis
happens later against the frozen snapshot.

Usage:  .venv/bin/python analytics/fetch_snapshot.py
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

GAMMA_CHUNK = 50      # ids per gamma /markets request (URL-length bound)
BOOKS_CHUNK = 500     # token_ids per POST /books (501+ -> HTTP 400)
CONCURRENCY = 8
RETRIES = 3
TIMEOUT = 30          # seconds per request
END_CURSORS = {"", "LTE="}  # CLOB end-of-pages sentinels

DATA_DIR = Path(__file__).parent / "data"


def _request(method: str, url: str, **kw) -> requests.Response:
    """One HTTP call with retries on 429/5xx/connection errors."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 0)) or 1.5 * (attempt + 1)
                time.sleep(wait)
                last = RuntimeError(f"HTTP {r.status_code} from {url}")
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}: {last}")


# --- Phase 1: sampling-markets (sequential cursor pagination) ---------------

def fetch_sampling_markets() -> list[dict]:
    rows: list[dict] = []
    cursor: str | None = None  # first request carries NO cursor param
    pages = 0
    while True:
        params = {"next_cursor": cursor} if cursor else {}
        page = _request("GET", f"{CLOB}/sampling-markets", params=params).json()
        data = page.get("data") or []
        rows.extend(data)
        pages += 1
        cursor = page.get("next_cursor") or ""
        print(f"  page {pages}: +{len(data)} rows (cursor={cursor!r})")
        if cursor in END_CURSORS:
            return rows


# --- Phase 2: gamma metadata (parallel chunks) -------------------------------

def fetch_gamma_chunk(ids: list[str]) -> list[dict]:
    params = [("limit", str(len(ids)))] + [("condition_ids", i) for i in ids]
    return _request("GET", f"{GAMMA}/markets", params=params).json()


# --- Phase 3: books (parallel batches) ---------------------------------------

def fetch_books_chunk(token_ids: list[str]) -> list[dict]:
    body = [{"token_id": t} for t in token_ids]
    return _request("POST", f"{CLOB}/books", json=body).json()


def yes_token(row: dict) -> str:
    """YES-preferred token id, else first non-empty (engine's YesToken rule)."""
    tokens = row.get("tokens") or []
    for t in tokens:
        if str(t.get("outcome", "")).strip().lower() == "yes" and t.get("token_id"):
            return str(t["token_id"])
    for t in tokens:
        if t.get("token_id"):
            return str(t["token_id"])
    return ""


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = DATA_DIR / ts
    out.mkdir(parents=True)
    t0 = time.monotonic()

    print(f"snapshot -> {out}")
    print("phase 1: CLOB /sampling-markets ...")
    sampling = fetch_sampling_markets()
    t1 = time.monotonic()
    (out / "sampling_markets.json").write_text(json.dumps(sampling))
    print(f"  {len(sampling)} rows in {t1 - t0:.1f}s")

    cids = [r["condition_id"] for r in sampling if r.get("condition_id")]
    tok_by_cid = {r["condition_id"]: yes_token(r) for r in sampling if r.get("condition_id")}
    tokens = [t for t in tok_by_cid.values() if t]

    gamma_chunks = [cids[i:i + GAMMA_CHUNK] for i in range(0, len(cids), GAMMA_CHUNK)]
    book_chunks = [tokens[i:i + BOOKS_CHUNK] for i in range(0, len(tokens), BOOKS_CHUNK)]
    print(f"phase 2+3 (parallel): {len(gamma_chunks)} gamma chunks + {len(book_chunks)} book batches ...")

    gamma_rows: list[dict] = []
    book_rows: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(fetch_gamma_chunk, c): ("gamma", i) for i, c in enumerate(gamma_chunks)}
        futs |= {pool.submit(fetch_books_chunk, c): ("books", i) for i, c in enumerate(book_chunks)}
        for fut in as_completed(futs):
            kind, idx = futs[fut]
            try:
                res = fut.result()
                (gamma_rows if kind == "gamma" else book_rows).extend(res)
            except Exception as e:  # keep going; coverage check reports the hole
                errors.append(f"{kind}[{idx}]: {e}")
    t2 = time.monotonic()

    (out / "gamma_markets.json").write_text(json.dumps(gamma_rows))
    (out / "books.json").write_text(json.dumps(book_rows))

    # --- verification ---------------------------------------------------------
    live = [r for r in sampling
            if r.get("active") and r.get("accepting_orders") and not r.get("closed")]

    def daily_rate(r: dict) -> float:
        rates = (r.get("rewards") or {}).get("rates") or []
        return sum(float(x.get("rewards_daily_rate") or 0) for x in rates)

    total_all = sum(daily_rate(r) for r in sampling)
    total_live = sum(daily_rate(r) for r in live)
    assets = sorted({str(x.get("asset_address", "")).lower()
                     for r in sampling
                     for x in ((r.get("rewards") or {}).get("rates") or [])})

    gamma_cids = {r.get("conditionId") for r in gamma_rows}
    gamma_missing = [c for c in cids if c not in gamma_cids]
    book_tokens = {str(b.get("asset_id", "")) for b in book_rows}
    books_missing = [c for c, t in tok_by_cid.items() if t and t not in book_tokens]
    no_token = [c for c, t in tok_by_cid.items() if not t]

    # Exit criteria (CI-friendly): a handful of venue-side gaps is NORMAL
    # (brand-new in-game markets missing from Gamma / without a book — verified
    # live 2026-07-02: 2-4 gaps, ~0.03% of the pool). Fail only on request-level
    # errors after retries, or coverage below 99% (a silently truncated
    # universe must NOT deploy).
    frac_gamma = len(gamma_rows) / max(1, len(cids))
    frac_books = len(book_rows) / max(1, len(tokens))

    manifest = {
        "ts_utc": ts,
        "elapsed_s": {"sampling": round(t1 - t0, 1), "gamma+books": round(t2 - t1, 1),
                      "total": round(t2 - t0, 1)},
        "sampling": {"rows": len(sampling), "live": len(live),
                     "total_daily_rate_all": total_all, "total_daily_rate_live": total_live,
                     "reward_asset_addresses": assets},
        "gamma": {"requested": len(cids), "returned": len(gamma_rows),
                  "coverage": round(frac_gamma, 4),
                  "missing": len(gamma_missing), "missing_cids": gamma_missing[:20]},
        "books": {"requested": len(tokens), "returned": len(book_rows),
                  "coverage": round(frac_books, 4),
                  "missing": len(books_missing), "missing_cids": books_missing[:20],
                  "markets_without_token": len(no_token)},
        "errors": errors,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(json.dumps(manifest, indent=2))
    ok = not errors and frac_gamma >= 0.99 and frac_books >= 0.99
    print(f"\nsnapshot {'OK' if ok else 'FAILED (fetch errors or coverage < 99%) — see manifest'} -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
