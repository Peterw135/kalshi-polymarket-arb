"""Match markets, re-price matched pairs from live order books, find arbitrage.

Kalshi is always one side. The other side is either polymarket.com ("polymarket")
or Polymarket US ("polymarket_us").
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import kalshi, polymarket, polymarket_us
from .arbitrage import strategies, top_of_book, size
from .matching import match_all, tier, LEAGUES, POLY_US_LEAGUES

MAX_DEPTH_FETCHES = 80  # order book calls per scan and venue (Kalshi basic tier: ~20 reads/s)
VENUE_LEAGUES = {"polymarket": LEAGUES, "polymarket_us": POLY_US_LEAGUES}


def _leg_dict(leg) -> dict:
    return {"venue": leg.venue, "side": leg.side, "label": leg.label, "ask": leg.ask,
            "fee_rate": leg.fee_rate, "depth_checked": leg.depth_checked,
            "ladder": leg.ladder[:15]}


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T")) if ts else None
    except ValueError:
        return None


def _top(m: dict) -> dict:
    """Top-of-book ladders from the market list (size None = unknown)."""
    return {"yes": [(m["yes_ask"], m.get("yes_ask_size"))] if m["yes_ask"] else [],
            "no": [(m["no_ask"], m.get("no_ask_size"))] if m["no_ask"] else []}


def _mid(lad: dict) -> float | None:
    """Implied YES probability from whatever asks exist."""
    y, n = lad["yes"], lad["no"]
    if y and n:
        return (y[0][0] + 1 - n[0][0]) / 2
    if y:
        return y[0][0]
    if n:
        return 1 - n[0][0]
    return None


def _best(k, p, mt, k_lad, p_lad, k_checked, venue, p_checked):
    strats = strategies(k, p, mt.inverted, k_lad, p_lad, k_checked, venue, p_checked)
    priced = [x for x in ((s, top_of_book(s)) for s in strats) if x[1]]
    return max(priced, key=lambda x: x[1]["edge"]) if priced else None


def scan(kalshi_markets: list[dict], other_markets: list[dict], venue: str = "polymarket",
         log=print) -> list[dict]:
    t0 = time.time()
    K = {m["id"]: m for m in kalshi_markets}
    k_outcomes: dict[str, int] = {}  # markets per Kalshi event page
    for m in kalshi_markets:
        k_outcomes[m["event_id"]] = k_outcomes.get(m["event_id"], 0) + 1
    P = {m["id"]: m for m in other_markets}
    matches = match_all(kalshi_markets, other_markets, VENUE_LEAGUES[venue])
    log(f"{venue}: matched {len(matches)} pairs in {time.time() - t0:.1f}s")

    # Only pre-game sports pairs. Postponed/cancelled games can settle differently on
    # each venue, and live/finished games move faster than a scan, so their "edges"
    # are not locked in.
    now = datetime.now(timezone.utc)

    # Postponements are often announced days ahead without any status in the API.
    # On polymarket.com an unplayed game resolves the "draw" market YES (and both team
    # markets NO), so a draw priced above 90% before kickoff means "expected not to be
    # played". Kalshi settles those at a "fair price" instead: not a hedge.
    likely_postponed = set()
    for m in other_markets:
        if m.get("market_type") == "moneyline" and m["slug"].endswith("-draw"):
            lad = _top(m)
            yes = _mid(lad)
            start = _parse(m.get("start_time"))
            if yes is not None and yes > 0.90 and (start is None or start > now):
                likely_postponed.add(m["series"])
    if likely_postponed:
        log(f"{venue}: {len(likely_postponed)} games look postponed (draw > 90% pre-game): "
            + ", ".join(sorted(likely_postponed)[:5]))

    def tradable(mt) -> bool:
        p = P[mt.poly_id]
        if p.get("status") or p.get("series") in likely_postponed:
            return False
        if mt.method in ("game", "spread", "total"):
            start = _parse(p.get("start_time"))
            if start and start <= now:
                return False
        return True

    before = len(matches)
    matches = [mt for mt in matches if tradable(mt)]
    log(f"{venue}: dropped {before - len(matches)} started/postponed/finished games")

    # 1. Price every pair at the top of book. polymarket.com: live CLOB books for all
    #    matched markets (Gamma's bestBid/Ask can lag). Polymarket US: the quotes from
    #    the market list, refreshed from its book below for pairs that look profitable.
    books = {}
    if venue == "polymarket":
        tokens = sorted({t for mt in matches for t in P[mt.poly_id]["tokens"]})
        try:
            books = polymarket.fetch_books(tokens)
        except Exception as e:
            log(f"polymarket books failed: {e}")

    def p_ladders(p):
        if venue == "polymarket" and p["tokens"] and p["tokens"][0] in books:
            return {"yes": books.get(p["tokens"][0], []), "no": books.get(p["tokens"][1], [])}, True
        return _top(p), False

    rows = []
    for mt in matches:
        k, p = K[mt.kalshi_id], P[mt.poly_id]
        pl, p_checked = p_ladders(p)
        # Equivalent markets can't stay far apart for long; a big gap usually means the
        # questions differ (different threshold, date, rules), not free money.
        km, pm = _mid(_top(k)), _mid(pl)
        if km is not None and pm is not None:
            gap = abs(km - (1 - pm if mt.inverted else pm))
            if gap > 0.15:
                mt.confidence = round(mt.confidence * (0.5 if gap > 0.30 else 0.75), 3)
                mt.reason += f" · prices disagree by {gap * 100:.0f}¢, so verify these are the same question"
        rows.append({"match": mt, "k": k, "p": p, "pl": pl, "p_checked": p_checked,
                     "best": _best(k, p, mt, _top(k), pl, False, venue, p_checked)})

    # 2. Full order book depth for pairs that look profitable at the top of book.
    hot = sorted((r for r in rows if r["best"] and r["best"][1]["edge"] > 0),
                 key=lambda r: -r["best"][1]["edge"])[:MAX_DEPTH_FETCHES]

    def fetch_depth(r):
        try:
            k_lad = kalshi.fetch_ask_ladders(r["k"]["id"])
        except Exception:
            k_lad = None
        p_lad = None
        if venue == "polymarket_us":
            try:
                p_lad = polymarket_us.fetch_ask_ladders(r["p"]["id"])
            except Exception:
                pass
        return r, k_lad, p_lad

    with ThreadPoolExecutor(4) as ex:
        for r, k_lad, p_lad in ex.map(fetch_depth, hot):
            if p_lad is not None:
                r["pl"], r["p_checked"] = p_lad, True
            r["best"] = _best(r["k"], r["p"], r["match"], k_lad or _top(r["k"]), r["pl"],
                              k_lad is not None, venue, r["p_checked"])

    out = []
    for r in rows:
        mt, k, p, best = r["match"], r["k"], r["p"], r["best"]
        if not best:
            continue
        legs, tob = best
        out.append({
            "id": f"{venue}|{k['id']}|{p['id']}",
            "venue": venue,
            "confidence": mt.confidence,
            "tier": tier(mt.confidence),
            "method": mt.method,
            "reason": mt.reason,
            "inverted": mt.inverted,
            "kalshi": {**{key: k[key] for key in ("id", "title", "outcome", "event_title", "url", "close_time",
                                                  "yes_bid", "yes_ask", "no_bid", "no_ask", "volume_24h")},
                       "page_outcomes": k_outcomes.get(k["event_id"], 1)},
            "polymarket": {key: p[key] for key in ("id", "title", "outcome", "event_title", "url", "close_time",
                                                   "yes_label", "no_label", "volume_24h", "liquidity")},
            "legs": [_leg_dict(l) for l in legs],
            **tob,
        })
    out.sort(key=lambda r: -r["edge"])
    log(f"{venue}: scan done in {time.time() - t0:.1f}s, {sum(r['edge'] > 0 for r in out)} with positive edge")
    return out


def size_row(row: dict, budget: float) -> dict:
    from .arbitrage import Leg
    legs = tuple(Leg(l["venue"], l["side"], l["label"], l["fee_rate"], [tuple(x) for x in l["ladder"]],
                     l["depth_checked"]) for l in row["legs"])
    return size(legs, budget)
