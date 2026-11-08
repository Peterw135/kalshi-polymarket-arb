"""Kalshi x Polymarket arbitrage scanner: fetch, match, price, serve.

    python3 server.py            # http://localhost:8000
    python3 server.py --port 9000 --refresh 120 --poly-min-volume 5000

Stdlib only. Snapshots are cached to data/*.json so a restart shows data instantly.
"""
import argparse
import hashlib
import os
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from arb import kalshi, polymarket, polymarket_us
from arb.matching import TIERS
from arb.scanner import scan, size_row

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
# Saved snapshots are only reused by the same code: any change to the scanner or
# fetchers invalidates them, so a restart after an update never shows stale logic.
CACHE_VERSION = hashlib.sha1(b"".join(
    f.read_bytes() for f in sorted([Path(__file__), *(Path(__file__).parent / "arb").glob("*.py")])
)).hexdigest()[:12]

SORT_KEYS = {"volume_24h", "volume", "liquidity", "close_time", "yes_ask", "title"}
TIER_MIN = dict(TIERS) | {"all": 0.0}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Latest snapshots, scan results and status for the UI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.markets: dict[str, list[dict]] = {"kalshi": [], "polymarket": [], "polymarket_us": []}
        self.opps: list[dict] = []
        self.status: dict[str, dict] = {
            p: {"state": "idle", "updated_at": None, "count": 0, "progress": "", "error": None, "duration_s": None,
                "from_cache": False}
            for p in ("kalshi", "polymarket", "polymarket_us", "scan")
        }

    def _read(self, name):
        f = DATA / f"{name}.json"
        if not f.exists():
            return None
        snap = json.loads(f.read_text())
        return snap if snap.get("version") == CACHE_VERSION else None

    def _write(self, name, updated_at, rows):
        tmp = DATA / f"{name}.json.tmp"
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "updated_at": updated_at, "rows": rows}))
        tmp.replace(DATA / f"{name}.json")

    def load_cache(self):
        for name in ("kalshi", "polymarket", "polymarket_us", "scan"):
            snap = self._read(name)
            if not snap:
                continue
            if name == "scan":
                self.opps = snap["rows"]
            else:
                self.markets[name] = snap["rows"]
            self.status[name].update(updated_at=snap["updated_at"], count=len(snap["rows"]), state="cached",
                                     from_cache=True)

    def set_status(self, name, **kw):
        with self.lock:
            self.status[name].update(kw)

    def save(self, name, rows: list[dict], duration: float):
        ts = now_iso()
        with self.lock:
            if name == "scan":
                self.opps = rows
            else:
                self.markets[name] = rows
            self.status[name].update(state="ok", updated_at=ts, count=len(rows),
                                     progress="", error=None, duration_s=round(duration, 1), from_cache=False)
        self._write(name, ts, rows)


STORE = Store()


def fetch_one(name, fetch):
    STORE.set_status(name, state="fetching", progress="starting")
    t0 = time.time()
    try:
        rows = [m.to_dict() for m in fetch(on_progress=lambda page, n: STORE.set_status(
            name, progress=f"page {page}, {n:,} markets"))]
        STORE.save(name, rows, time.time() - t0)
        print(f"[{name}] {len(rows):,} markets in {time.time() - t0:.1f}s")
    except Exception as e:  # keep serving the last good snapshot
        traceback.print_exc()
        STORE.set_status(name, state="error", error=str(e), progress="")


def scan_loop(interval: int, poly_min_volume: float):
    while True:
        with ThreadPoolExecutor(3) as ex:
            ex.submit(fetch_one, "kalshi", kalshi.fetch_markets)
            ex.submit(fetch_one, "polymarket",
                      lambda **kw: polymarket.fetch_markets(poly_min_volume, **kw))
            ex.submit(fetch_one, "polymarket_us", polymarket_us.fetch_markets)
        with STORE.lock:
            k = STORE.markets["kalshi"]
            others = {v: STORE.markets[v] for v in ("polymarket", "polymarket_us")}
        if k and any(others.values()):
            STORE.set_status("scan", state="fetching", progress="matching + pricing")
            t0 = time.time()
            try:
                log = lambda msg: (print(f"[scan] {msg}"), STORE.set_status("scan", progress=msg))
                rows = []
                for venue, markets in others.items():
                    if markets:
                        rows += scan(k, markets, venue, log=log)
                rows.sort(key=lambda r: -r["edge"])
                STORE.save("scan", rows, time.time() - t0)
            except Exception as e:
                traceback.print_exc()
                STORE.set_status("scan", state="error", error=str(e), progress="")
        time.sleep(interval)


def query_markets(platform: str, q: str, sort: str, direction: str, limit: int, offset: int):
    with STORE.lock:
        rows = STORE.markets.get(platform, [])
    terms = q.lower().split()
    if terms:
        rows = [r for r in rows
                if all(t in f"{r['title']} {r['event_title']} {r['outcome']} {r['id']}".lower() for t in terms)]
    key = sort if sort in SORT_KEYS else "volume_24h"
    desc = direction != "asc"
    # Rows missing the sort field always go last.
    present = [r for r in rows if r[key] is not None]
    absent = [r for r in rows if r[key] is None]
    rows = sorted(present, key=lambda r: r[key], reverse=desc) + absent
    slim = [{k: v for k, v in r.items() if k not in ("tokens",)} for r in rows[offset:offset + limit]]
    return {"total": len(rows), "markets": slim}


def query_opps(q: str, min_tier: str, min_edge: float, budget: float, sort: str, limit: int, venue: str = "all"):
    with STORE.lock:
        rows = STORE.opps
    floor = TIER_MIN.get(min_tier, TIER_MIN["medium"])
    terms = q.lower().split()
    out = []
    for r in rows:
        if r["confidence"] < floor or r["edge"] < min_edge:
            continue
        if venue != "all" and r.get("venue", "polymarket") != venue:
            continue
        if terms:
            hay = f"{r['kalshi']['title']} {r['kalshi']['outcome']} {r['kalshi']['event_title']} " \
                  f"{r['polymarket']['title']} {r['polymarket']['event_title']}".lower()
            if not all(t in hay for t in terms):
                continue
        sized = size_row(r, budget) if r["edge"] > 0 else None
        legs = [{**l, "ladder": l["ladder"][:5]} for l in r["legs"]]
        out.append({**r, "legs": legs, "sized": sized})
    if sort == "profit":
        out.sort(key=lambda r: -((r["sized"] or {}).get("profit") or -1))
    elif sort == "confidence":
        out.sort(key=lambda r: (-r["confidence"], -r["edge"]))
    else:
        out.sort(key=lambda r: -r["edge"])
    matched = sum(1 for r in rows if venue == "all" or r.get("venue", "polymarket") == venue)
    return {"total": len(out), "matched": matched, "rows": out[:limit]}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # Never let the browser serve a stale copy of the page after an update.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/status":
                with STORE.lock:
                    return self._json(STORE.status)
            if u.path == "/api/markets":
                platform = qs.get("platform", "kalshi")
                if platform not in STORE.markets:
                    return self._json({"error": "unknown platform"}, 400)
                return self._json(query_markets(
                    platform, qs.get("q", ""), qs.get("sort", "volume_24h"), qs.get("dir", "desc"),
                    min(int(qs.get("limit", 100)), 1000), int(qs.get("offset", 0))))
            if u.path == "/api/opportunities":
                return self._json(query_opps(
                    qs.get("q", ""), qs.get("tier", "medium"), float(qs.get("min_edge", 0)),
                    max(float(qs.get("budget", 1000)), 0), qs.get("sort", "edge"),
                    min(int(qs.get("limit", 200)), 2000), qs.get("venue", "all")))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        return super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="0.0.0.0 to accept outside connections (set automatically in Docker)")
    ap.add_argument("--refresh", type=int, default=60, help="seconds to wait between scans")
    ap.add_argument("--poly-min-volume", type=float, default=10_000,
                    help="skip Polymarket markets with lifetime volume below this (USD)")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    STORE.load_cache()
    threading.Thread(target=scan_loop, args=(args.refresh, args.poly_min_volume), daemon=True).start()

    print(f"Serving on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
