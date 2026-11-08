"""Polymarket US (CFTC-regulated exchange) public market data. No auth needed.

Docs: https://docs.polymarket.us/api-reference/introduction
Public gateway: https://gateway.polymarket.us

Unlike polymarket.com there is ONE order book per market (like Kalshi): the YES
side is "long", buying NO means hitting a YES bid, so NO asks = 1 - YES bids.

  /v1/events   events with their main markets, team names/abbreviations and game
               status (period / live). ~10s for everything.
  /v1/markets  every market, including the spreads and totals that /v1/events omits.
  /v1/markets/{slug}/book   full order book.

Fees (CFTC filing): fee = Θ * C * p * (1 - p), Θ = the market's `feeCoefficient`.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .http import get_json
from .models import Market

BASE = "https://gateway.polymarket.us"
WEB = "https://polymarket.us"

# Event `period` values: NS = not started, "" = not a game, FT = finished, the rest = in play.
NOT_STARTED = {"NS", ""}
POSTPONED = {"POST", "PST", "PPD", "POSTPONED", "DELAYED", "SUSP", "INT"}
CANCELLED = {"CANC", "CAN", "CANCELLED", "CANCELED", "ABD", "AWD", "WO"}
FINISHED = {"FT", "AET", "PEN", "END", "ENDED", "FINAL", "F"}


def _f(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quote(v) -> float | None:
    x = _f(v)
    return None if x is None or x <= 0 or x >= 1 else x


def _status(ev: dict) -> str:
    period = str(ev.get("period") or "").upper()
    if period in POSTPONED:
        return "postponed"
    if period in CANCELLED:
        return "cancelled"
    if period in FINISHED or ev.get("ended"):
        return "ended"
    if ev.get("live") or period not in NOT_STARTED:
        return "live"
    return ""


def _market_type(t: str) -> str:
    """Map Polymarket US sports types onto the matcher's moneyline/spreads/totals."""
    t = t or ""
    if t.endswith(("_full_game_winner", "_full_time_winner", "_match_winner", "_fight_winner")):
        return "moneyline"
    if t.endswith("_team_full_game_spread"):
        return "spreads"
    if t.endswith("_team_full_game_total") and "points" not in t:
        return "totals"
    return t  # other props: non-empty, so the text matcher skips them


def _paged(path: str, key: str, page: int = 500, on_page=None) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = get_json(f"{BASE}{path}", {"limit": page, "offset": offset,
                                          "active": "true", "closed": "false"}).get(key) or []
        out += rows
        offset += len(rows)
        if on_page:
            on_page(len(out))
        if len(rows) < page:
            return out


def _question(m: dict) -> str:
    """Full yes/no question. `question` is often generic ("Major League Soccer Champion");
    the description's first sentence is usually the real one."""
    desc = (m.get("description") or "").strip()
    first = re.split(r"(?<=\?)\s", desc, maxsplit=1)[0]
    if first.startswith("Will ") and first.endswith("?") and len(first) < 300:
        return re.sub(r",? scheduled (?:to|for) [^?]*", "", first).rstrip("?") + "?"
    q, t = m.get("question") or "", m.get("title") or ""
    return f"{q} {t}".strip() if t and t.lower() not in q.lower() else q


def _event_title(ev: dict) -> str:
    """"Orlando Magic vs. Golden State Warriors" rather than "ORL Magic vs. GS Warriors":
    full team names, in the order of the slug (nba-orl-gs-2026-11-07)."""
    title = ev.get("title") or ""
    teams = {(t.get("abbreviation") or "").lower(): t.get("name") for t in ev.get("teams") or []}
    g = re.match(r"^[a-z0-9]+-([a-z0-9]+)-([a-z0-9]+)-\d{4}-\d{2}-\d{2}$", ev.get("slug") or "")
    if g and len(teams) == 2 and all(teams.get(x) for x in g.groups()):
        return f"{teams[g.group(1)]} vs. {teams[g.group(2)]}"
    return title


def fetch_markets(on_progress=None) -> list[Market]:
    with ThreadPoolExecutor(2) as ex:
        f_ev = ex.submit(_paged, "/v1/events", "events")
        f_mk = ex.submit(_paged, "/v1/markets", "markets",
                         on_page=lambda n: on_progress and on_progress(n // 500, n))
        events, markets = f_ev.result(), f_mk.result()

    ev_by_slug = {e["slug"]: e for e in events if e.get("slug")}
    ev_of_market = {m["slug"]: e for e in events for m in e.get("markets") or [] if m.get("slug")}
    slugs_by_len = sorted(ev_by_slug, key=len, reverse=True)

    def event_for(m) -> dict | None:
        if m["slug"] in ev_of_market:
            return ev_of_market[m["slug"]]
        # e.g. "asc-cfb-mia-nd-2026-11-07-neg-23pt5" -> event "cfb-mia-nd-2026-11-07"
        rest = m["slug"].split("-", 1)[-1]
        for s in slugs_by_len:
            if rest == s or rest.startswith(s + "-"):
                return ev_by_slug[s]
        return None

    out: list[Market] = []
    for m in markets:
        if m.get("status") != "MARKET_STATUS_OPEN" or m.get("hidden"):
            continue
        yes_bid, yes_ask = _quote(m.get("bestBidQuote")), _quote(m.get("bestAskQuote"))
        if yes_bid is None and yes_ask is None:
            continue
        ev = event_for(m) or {}
        sides = m.get("marketSides") or []
        long = next((s for s in sides if s.get("long")), {})
        short = next((s for s in sides if not s.get("long")), {})
        yes_label = long.get("description") or "Yes"
        no_label = short.get("description") or "No"
        mtype = _market_type(m.get("sportsMarketType") or "")
        ev_title = _event_title(ev) or m.get("question") or ""
        team = (long.get("team") or {}).get("name") or ""
        title, outcome = _question(m), m.get("title") or ""

        if mtype == "moneyline":
            if yes_label.lower() == "yes":  # soccer 3-way: one market per team + draw
                outcome = "Draw" if m["slug"].endswith("-draw") else (team or outcome)
            else:                           # two-way: YES = the "long" side's team
                # Button labels are nicknames ("Bison"); use full names so the matcher
                # can tell which Kalshi team each side is ("Howard Bison" vs "Penn").
                yes_label = team or yes_label
                no_label = (short.get("team") or {}).get("name") or no_label
                title, outcome = ev_title, yes_label
        elif mtype == "spreads":
            line = _f(m.get("line"))
            if line is None or line >= 0 or not team:
                continue  # only "X covers -N" == Kalshi "X wins by over N"
            other = (short.get("team") or {}).get("name") or no_label
            title, outcome, yes_label, no_label = f"Spread: {team} ({line:g})", team, team, other
        elif mtype == "totals":
            line = _f(m.get("line"))
            if line is None:
                continue
            title, outcome = f"{ev_title}: O/U {line:g}", "Over"

        out.append(Market(
            platform="polymarket_us",
            id=m["slug"],
            title=title,
            event_title=ev_title,
            outcome=outcome,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=round(1 - yes_ask, 4) if yes_ask is not None else None,
            no_ask=round(1 - yes_bid, 4) if yes_bid is not None else None,
            last_price=None,
            volume=0.0,
            volume_24h=0.0,
            liquidity=None,
            close_time=m.get("endDate"),
            category=m.get("category") or "",
            url=f"{WEB}/event/{ev['slug']}" if ev.get("slug") else WEB,
            event_id=str(ev.get("id") or ""),
            series=ev.get("slug") or "",
            slug=m["slug"],
            yes_label=yes_label,
            no_label=no_label,
            market_type=mtype,
            start_time=m.get("gameStartTime") or ev.get("startTime"),
            fee_rate=_f(m.get("feeCoefficient")) or 0.0,
            status=_status(ev),
        ))
    return out


def fetch_ask_ladders(slug: str) -> dict[str, list[tuple[float, float]]]:
    """Executable asks per side, best first: YES = offers, NO = 1 - bids."""
    data = (get_json(f"{BASE}/v1/markets/{slug}/book") or {}).get("marketData") or {}

    def lv(rows, flip):
        out = []
        for r in rows or []:
            p, q = _f(r.get("px")), _f(r.get("qty"))
            if p is None or not q:
                continue
            p = round(1 - p, 4) if flip else p
            if 0 < p < 1:
                out.append((p, q))
        return sorted(out, key=lambda x: x[0])

    return {"yes": lv(data.get("offers"), False), "no": lv(data.get("bids"), True)}
