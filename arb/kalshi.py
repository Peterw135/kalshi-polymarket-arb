"""Kalshi public market data (Trade API v2, no auth needed for reads).

Docs: https://docs.kalshi.com/
We page through /events with nested markets instead of /markets, because
/markets is dominated by auto-generated multivariate parlay markets
(KXMVE...) that have no counterpart on Polymarket.
"""
import re

from .http import get_json
from .models import Market

BASE = "https://api.elections.kalshi.com/trade-api/v2"
BASE_FEE = 0.07  # taker fee = ceil_to_cent(0.07 * multiplier * C * P * (1 - P))


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quote(v) -> float | None:
    """Kalshi reports an empty side as 0 (bid) or 1 (ask); treat those as no quote."""
    x = _f(v)
    return None if x is None or x <= 0 or x >= 1 else x


def fetch_fee_multipliers() -> dict[str, float]:
    """series_ticker -> fee multiplier (1 for almost everything, 0.5 / 0 for a few)."""
    data = get_json(f"{BASE}/series")
    return {s["ticker"]: float(s.get("fee_multiplier") if s.get("fee_multiplier") is not None else 1)
            for s in data.get("series") or []}


def fetch_markets(max_pages: int = 500, quoted_only: bool = True, on_progress=None) -> list[Market]:
    try:
        fee_mult = fetch_fee_multipliers()
    except Exception:
        fee_mult = {}
    out: list[Market] = []
    cursor = None
    for page in range(max_pages):
        data = get_json(f"{BASE}/events", {
            "limit": 200,
            "status": "open",
            "with_nested_markets": "true",
            "cursor": cursor,
        })
        events = data.get("events") or []
        for ev in events:
            series_ticker = ev.get("series_ticker") or ""
            slug = re.sub(r"[^a-z0-9]+", "-", (ev.get("title") or "market").lower()).strip("-") or "market"
            base = f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}" if series_ticker else ""
            for m in ev.get("markets") or []:
                if m.get("market_type") != "binary" or m.get("status") != "active":
                    continue
                yes_bid, yes_ask = _quote(m.get("yes_bid_dollars")), _quote(m.get("yes_ask_dollars"))
                if quoted_only and yes_bid is None and yes_ask is None:
                    continue
                out.append(Market(
                    platform="kalshi",
                    id=m["ticker"],
                    title=m.get("title") or ev.get("title") or "",
                    event_title=ev.get("title") or "",
                    outcome=m.get("yes_sub_title") or "",
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=_quote(m.get("no_bid_dollars")),
                    no_ask=_quote(m.get("no_ask_dollars")),
                    last_price=_f(m.get("last_price_dollars")),
                    volume=_f(m.get("volume_fp")) or 0.0,
                    volume_24h=_f(m.get("volume_24h_fp")) or 0.0,
                    liquidity=_f(m.get("open_interest_fp")),
                    close_time=m.get("close_time"),
                    category=ev.get("category") or "",
                    # Event page. Kalshi can't deep-link an outcome (it always preselects its
                    # first row, e.g. "Tie"), so the UI tells you which row to click.
                    url=f"{base}/{(m.get('event_ticker') or ev.get('event_ticker') or '').lower()}" if base else "https://kalshi.com",
                    event_id=m.get("event_ticker") or ev.get("event_ticker") or "",
                    series=series_ticker,
                    yes_label=m.get("yes_sub_title") or "Yes",
                    no_label=m.get("no_sub_title") or "No",
                    fee_rate=BASE_FEE * fee_mult.get(series_ticker, 1.0),
                    yes_ask_size=_f(m.get("yes_ask_size_fp")),
                    # Buying NO lifts resting YES bids.
                    no_ask_size=_f(m.get("yes_bid_size_fp")),
                ))
        if on_progress:
            on_progress(page + 1, len(out))
        cursor = data.get("cursor")
        if not cursor or not events:
            break
    return out


def fetch_ask_ladders(ticker: str, depth: int = 20) -> dict[str, list[tuple[float, float]]]:
    """Executable asks for each side, best first, as (price, contracts).

    Kalshi's book only lists bids. A YES bid at p is a NO offer at 1 - p and vice
    versa, so the YES asks are 1 - (NO bids) and the NO asks are 1 - (YES bids).
    """
    book = get_json(f"{BASE}/markets/{ticker}/orderbook", {"depth": depth}).get("orderbook_fp") or {}

    def asks(opposite_bids):
        lv = [(round(1 - float(p), 4), float(s)) for p, s in opposite_bids or []]
        return sorted((x for x in lv if 0 < x[0] < 1 and x[1] > 0), key=lambda x: x[0])

    return {"yes": asks(book.get("no_dollars")), "no": asks(book.get("yes_dollars"))}
