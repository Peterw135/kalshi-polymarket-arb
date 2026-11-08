"""Run one full scan and write a static site (for GitHub Pages / any static host).

    python3 scan_once.py --out site

Writes:
    site/index.html               the UI (runs in "static mode": filters and sizes in the browser)
    site/data/opportunities.json  matched pairs with order-book ladders
    site/data/status.json         per-venue fetch status and timestamps

Exits non-zero if a venue fails to load or nothing is found, so a scheduled deploy
keeps the previous good snapshot instead of publishing an empty one.
"""
import argparse
import json
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from arb import kalshi, polymarket, polymarket_us
from arb.scanner import scan

ROOT = Path(__file__).parent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--poly-min-volume", type=float, default=10_000)
    ap.add_argument("--min-edge", type=float, default=-0.10,
                    help="drop pairs worse than this (keeps the file small; UI can still show 'no edge' pairs)")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "static" / "index.html", out / "index.html")
    (out / ".nojekyll").touch()

    status: dict[str, dict] = {}

    def fetch(name, fn):
        t0 = time.time()
        try:
            rows = [m.to_dict() for m in fn()]
            status[name] = {"state": "ok", "count": len(rows), "updated_at": now_iso(),
                            "duration_s": round(time.time() - t0, 1), "error": None, "progress": ""}
            print(f"[{name}] {len(rows):,} markets in {time.time() - t0:.1f}s", flush=True)
            return rows
        except Exception as e:
            traceback.print_exc()
            status[name] = {"state": "error", "count": 0, "updated_at": None, "error": str(e), "progress": ""}
            return []

    with ThreadPoolExecutor(3) as ex:
        fk = ex.submit(fetch, "kalshi", kalshi.fetch_markets)
        fp = ex.submit(fetch, "polymarket", lambda: polymarket.fetch_markets(args.poly_min_volume))
        fu = ex.submit(fetch, "polymarket_us", polymarket_us.fetch_markets)
        K, P, U = fk.result(), fp.result(), fu.result()

    t0 = time.time()
    rows = []
    for venue, markets in (("polymarket", P), ("polymarket_us", U)):
        if K and markets:
            rows += scan(K, markets, venue, log=lambda m: print(f"[scan] {m}", flush=True))
    rows = [r for r in rows if r["edge"] >= args.min_edge]
    rows.sort(key=lambda r: -r["edge"])
    status["scan"] = {"state": "ok", "count": len(rows), "updated_at": now_iso(),
                      "duration_s": round(time.time() - t0, 1), "error": None, "progress": ""}

    failed = [n for n, s in status.items() if s["state"] == "error"]
    if failed or not rows:
        print(f"Not publishing: failed={failed} rows={len(rows)}", file=sys.stderr)
        return 1

    (out / "data" / "opportunities.json").write_text(json.dumps(rows, separators=(",", ":")))
    (out / "data" / "status.json").write_text(json.dumps({"generated_at": now_iso(), "venues": status}))
    print(f"Wrote {len(rows):,} pairs to {out}/data", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
