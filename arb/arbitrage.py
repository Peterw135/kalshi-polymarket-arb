"""Price a matched pair as a two-leg arbitrage.

For an equivalent pair, buying YES on one venue and NO on the other pays exactly
$1 per contract whichever way the event resolves. If the two asks plus taker
fees cost less than $1, the difference is locked in (ignoring settlement risk).

    same polarity:  K YES + P NO    or   K NO + P YES
    inverted:       K YES + P YES   or   K NO + P NO     (Kalshi YES == Polymarket NO)

Fees (taker, per contract at price p):
    Kalshi:      ceil_to_cent(0.07 * multiplier * C * p * (1 - p))   per order
    Polymarket:  feeRate * C * p * (1 - p)   (feeRate from the market's feeSchedule; 0 if fees disabled)
    Polymarket US: ceil_to_cent(Θ * C * p * (1 - p)), Θ = the market's feeCoefficient
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Ladder = list  # [(price, size)], best (lowest) ask first. size None = unknown


@dataclass
class Leg:
    venue: str        # "kalshi" | "polymarket"
    side: str         # "yes" | "no"
    label: str        # human description of what this leg pays out on
    fee_rate: float
    ladder: Ladder = field(default_factory=list)
    depth_checked: bool = False  # ladder came from a full order book, not top-of-book

    @property
    def ask(self) -> float | None:
        """Best ask with at least one whole contract behind it (skips dust like 0.01 contracts)."""
        for price, qty in self.ladder:
            if qty is None or qty >= 1:
                return price
        return None


def unit_fee(rate: float, p: float) -> float:
    return rate * p * (1 - p)


def order_fee(venue: str, rate: float, p: float, contracts: float) -> float:
    raw = rate * contracts * p * (1 - p)
    # Kalshi and Polymarket US (both CFTC exchanges) charge whole cents, rounded up.
    return math.ceil(round(raw * 100, 6)) / 100 if venue in ("kalshi", "polymarket_us") else round(raw, 5)


def top_of_book(legs: tuple[Leg, Leg]) -> dict | None:
    a, b = legs
    if a.ask is None or b.ask is None:
        return None
    price = a.ask + b.ask
    fees = unit_fee(a.fee_rate, a.ask) + unit_fee(b.fee_rate, b.ask)
    cost = price + fees
    return {"price": round(price, 4), "fees": round(fees, 4), "cost": round(cost, 4),
            "edge": round(1 - cost, 4), "roi": round((1 - cost) / cost, 4) if cost else None}


def size(legs: tuple[Leg, Leg], budget: float) -> dict:
    """Walk both ask ladders while the marginal pair still costs < $1 after fees."""
    a, b = legs
    la, lb = [list(x) for x in a.ladder], [list(x) for x in b.ladder]
    i = j = 0
    contracts = spent = 0.0
    fills_a: dict[float, float] = {}
    fills_b: dict[float, float] = {}
    limited_by = "no edge"
    while i < len(la) and j < len(lb):
        pa, sa = la[i]
        pb, sb = lb[j]
        unit = pa + pb + unit_fee(a.fee_rate, pa) + unit_fee(b.fee_rate, pb)
        if unit >= 1:
            limited_by = "depth" if contracts else "no edge"
            break
        room = (budget - spent) / unit
        q = math.floor(min(x for x in (sa, sb, room) if x is not None))
        if q <= 0:
            if room < 1:
                limited_by = "budget"
                break
            # A fractional contract left on a level; skip past it.
            if sa is not None and sa < 1:
                i += 1
            if sb is not None and sb < 1:
                j += 1
            continue
        contracts += q
        spent += q * unit
        fills_a[pa] = fills_a.get(pa, 0) + q
        fills_b[pb] = fills_b.get(pb, 0) + q
        if sa is not None:
            la[i][1] -= q
        if sb is not None:
            lb[j][1] -= q
        if sa is not None and la[i][1] <= 1e-9:
            i += 1
        if sb is not None and lb[j][1] <= 1e-9:
            j += 1
        if sa is None or sb is None:
            limited_by = "budget (depth unknown)"
            break
    else:
        limited_by = "depth"

    if not contracts:
        return {"contracts": 0, "cost": 0, "fees": 0, "profit": 0, "limited_by": limited_by}
    # Exact fees: Kalshi rounds up to the cent per order; treat each price level as one order.
    fees_a = sum(order_fee(a.venue, a.fee_rate, p, q) for p, q in fills_a.items())
    fees_b = sum(order_fee(b.venue, b.fee_rate, p, q) for p, q in fills_b.items())
    fees = fees_a + fees_b
    outlay = sum(p * q for p, q in fills_a.items()) + sum(p * q for p, q in fills_b.items())
    cost = outlay + fees
    return {
        "contracts": contracts,
        "cost": round(cost, 2),
        "fees": round(fees, 2),
        "profit": round(contracts - cost, 2),
        "roi": round((contracts - cost) / cost, 4) if cost else None,
        "avg_price_a": round(sum(p * q for p, q in fills_a.items()) / contracts, 4),
        "avg_price_b": round(sum(p * q for p, q in fills_b.items()) / contracts, 4),
        # What to type into each venue's dollar box: shares x price + that venue's fee
        # (Kalshi's "Dollars" amount includes its fee).
        "spend_a": round(sum(p * q for p, q in fills_a.items()) + fees_a, 2),
        "spend_b": round(sum(p * q for p, q in fills_b.items()) + fees_b, 2),
        "fees_a": round(fees_a, 2),
        "fees_b": round(fees_b, 2),
        "limited_by": limited_by,
    }


def strategies(k: dict, p: dict, inverted: bool, k_ladders: dict, p_ladders: dict,
               k_checked: bool, p_venue: str = "polymarket", p_checked: bool = True) -> list[tuple[Leg, Leg]]:
    """Both ways to cover the event, each as (kalshi leg, polymarket leg)."""
    # Labels mirror each site's buttons. Kalshi: pick the outcome row, then Yes/No.
    # Polymarket two-way markets (e.g. "Nets vs. Hawks") label the buttons with team
    # names instead of Yes/No.
    def k_leg(side):
        label = f"{side.upper()} on “{k['yes_label']}”"
        return Leg("kalshi", side, label, k["fee_rate"], k_ladders.get(side) or [], k_checked)

    def p_leg(side):
        named = p["yes_label"].lower() != "yes"
        label = f"“{p['yes_label'] if side == 'yes' else p['no_label']}”" if named else side.upper()
        return Leg(p_venue, side, label, p["fee_rate"], p_ladders.get(side) or [], p_checked)

    if inverted:
        return [(k_leg("yes"), p_leg("yes")), (k_leg("no"), p_leg("no"))]
    return [(k_leg("yes"), p_leg("no")), (k_leg("no"), p_leg("yes"))]
