"""Normalized market shape shared by both venues.

All prices are in dollars per $1 contract (0.00 - 1.00). A price is None when
there is no quote on that side of the book.
"""
from dataclasses import dataclass, asdict, field


@dataclass
class Market:
    platform: str            # "kalshi" | "polymarket"
    id: str                  # Kalshi market ticker / Polymarket market id
    title: str               # the yes/no question
    event_title: str         # parent event title
    outcome: str             # outcome label within the event (e.g. a candidate), may be ""
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float            # lifetime volume (Kalshi: contracts, Polymarket: USD)
    volume_24h: float
    liquidity: float | None  # Polymarket liquidity USD / Kalshi open interest (contracts)
    close_time: str | None   # ISO-8601
    category: str
    url: str
    # --- fields used for matching / pricing ---
    event_id: str = ""       # Kalshi event_ticker / Polymarket event id
    series: str = ""         # Kalshi series ticker / Polymarket event slug
    slug: str = ""           # Polymarket market slug
    yes_label: str = "Yes"   # what YES means, e.g. "Nets" on a "Nets vs. Hawks" market
    no_label: str = "No"
    market_type: str = ""    # Polymarket sportsMarketType (moneyline/spreads/totals/...)
    start_time: str | None = None
    fee_rate: float = 0.0    # taker fee coefficient: fee = rate * C * p * (1 - p)
    tokens: list = field(default_factory=list)  # Polymarket CLOB token ids [yes, no]
    yes_ask_size: float | None = None  # top-of-book size, contracts
    no_ask_size: float | None = None
    status: str = ""         # game state from the venue: "", "live", "ended", "postponed", "cancelled"

    def to_dict(self) -> dict:
        return asdict(self)
