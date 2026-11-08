"""Polymarket public market data via the Gamma API (no auth needed).

Docs: https://docs.polymarket.com/
Gamma caps page size at 100 and offset paging at a few thousand, so we use
/markets/keyset (cursor paging). Gamma's bestBid / bestAsk can lag the real
order book, so matched markets are re-priced from the CLOB (`fetch_books`)
before any arbitrage is computed.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import urllib.request

from .http import get_json, USER_AGENT
from .models import Market

BASE = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quote(v) -> float | None:
    x = _f(v)
    return None if x is None or x <= 0 or x >= 1 else x


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except ValueError:
        return []


def _fee_rate(m: dict) -> float:
    if not m.get("feesEnabled"):
        return 0.0
    sched = m.get("feeSchedule") or {}
    rate = _f(sched.get("rate"))
    return rate if rate is not None else 0.05  # docs' "Other / General" rate


POSTPONED = {"POST", "PST", "PPD", "POSTPONED", "DELAYED", "SUSP", "SUSPENDED", "INT"}
CANCELLED = {"CANC", "CAN", "CANCELLED", "CANCELED", "ABD", "ABANDONED", "AWD", "WO"}
FINISHED = {"FT", "AET", "PEN", "END", "ENDED", "FINAL", "F", "F/OT", "VFT"}


def _game_status(ev: dict) -> str:
    period = str(ev.get("period") or "").upper()
    if period in POSTPONED:
        return "postponed"
    if period in CANCELLED:
        return "cancelled"
    if ev.get("ended") or period in FINISHED:
        return "ended"
    if ev.get("live"):
        return "live"
    return ""


def fetch_markets(min_volume: float = 10_000, max_pages: int = 300, on_progress=None) -> list[Market]:
    out: list[Market] = []
    cursor = None
    # Markets past their end date are usually just waiting on resolution.
    stale_before = datetime.now(timezone.utc) - timedelta(days=1)
    for page in range(max_pages):
        data = get_json(f"{BASE}/markets/keyset", {
            "limit": 100,
            "closed": "false",
            "active": "true",
            "archived": "false",
            "volume_num_min": min_volume or None,
            "after_cursor": cursor,
        })
        markets = data.get("markets") or []
        for m in markets:
            if not m.get("enableOrderBook") or not m.get("acceptingOrders", True):
                continue
            outcomes = _json_list(m.get("outcomes"))
            tokens = _json_list(m.get("clobTokenIds"))
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            end = m.get("endDate")
            try:
                if end and datetime.fromisoformat(end.replace("Z", "+00:00")) < stale_before:
                    continue
            except ValueError:
                pass
            yes_bid, yes_ask = _quote(m.get("bestBid")), _quote(m.get("bestAsk"))
            if yes_bid is None and yes_ask is None:
                continue
            ev = (m.get("events") or [{}])[0]
            ev_slug = ev.get("slug") or m.get("slug")
            yes_no = [o.lower() for o in outcomes] == ["yes", "no"]
            out.append(Market(
                platform="polymarket",
                id=str(m["id"]),
                title=m.get("question") or "",
                event_title=ev.get("title") or m.get("question") or "",
                # Non Yes/No binaries (e.g. "Nets vs. Hawks") put the YES side in outcomes[0].
                outcome=m.get("groupItemTitle") or ("" if yes_no else outcomes[0]),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=round(1 - yes_ask, 4) if yes_ask is not None else None,
                no_ask=round(1 - yes_bid, 4) if yes_bid is not None else None,
                last_price=_f(m.get("lastTradePrice")),
                volume=_f(m.get("volumeNum")) or 0.0,
                volume_24h=_f(m.get("volume24hr")) or 0.0,
                liquidity=_f(m.get("liquidityNum")),
                close_time=end,
                category=m.get("category") or "",
                url=f"https://polymarket.com/event/{ev_slug}/{m.get('slug')}" if ev_slug else "https://polymarket.com",
                event_id=str(ev.get("id") or ""),
                series=ev_slug or "",
                slug=m.get("slug") or "",
                yes_label=outcomes[0],
                no_label=outcomes[1],
                market_type=m.get("sportsMarketType") or "",
                start_time=m.get("gameStartTime"),
                fee_rate=_fee_rate(m),
                tokens=[str(t) for t in tokens],
                status=_game_status(ev),
            ))
        if on_progress:
            on_progress(page + 1, len(out))
        cursor = data.get("next_cursor")
        if not cursor or not markets:
            break
    return out


def fetch_books(token_ids: list[str], batch: int = 100, workers: int = 4) -> dict[str, list[tuple[float, float]]]:
    """token_id -> ask ladder [(price, shares)] best first, from the live CLOB."""
    chunks = [token_ids[i:i + batch] for i in range(0, len(token_ids), batch)]

    def fetch(chunk):
        req = urllib.request.Request(
            f"{CLOB}/books", data=json.dumps([{"token_id": t} for t in chunk]).encode(),
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    out: dict[str, list[tuple[float, float]]] = {}
    with ThreadPoolExecutor(workers) as ex:
        for books in ex.map(fetch, chunks):
            for b in books:
                asks = sorted(((float(a["price"]), float(a["size"])) for a in b.get("asks") or []),
                              key=lambda x: x[0])
                out[str(b.get("asset_id"))] = [a for a in asks if 0 < a[0] < 1 and a[1] > 0]
    return out
