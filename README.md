# Kalshi × Polymarket arbitrage scanner

Finds the same yes/no question listed on both venues and prices buying YES on one
and NO on the other. If both asks plus taker fees cost less than $1, the gap is
locked in whichever way the event resolves, as long as both venues settle the
question the same way.

```
python3 server.py              # http://localhost:8000  (Python 3.10+, no dependencies)
python3 server.py --refresh 60 --poly-min-volume 10000 --port 8000
```

## Pipeline (every `--refresh` seconds)

1. **Fetch.** Pulls all open Kalshi events (`/trade-api/v2/events`, ~150k quoted markets, ~35s)
   and Polymarket markets above `--poly-min-volume` (Gamma `/markets/keyset`).
2. **Match** (`arb/matching.py`):
   - **Games:** Kalshi tickers (`KXNBAGAME-26NOV07BKNATL-ATL`) and Polymarket slugs
     (`nba-bkn-atl-2026-11-07`) are paired by league, date, team abbreviations and names.
     Moneylines, spreads and full-game totals are then paired inside each game.
     "Inverted" pairs (Kalshi "Atlanta wins" = Polymarket NO on "Nets") are only made
     when the game can't end in a draw.
   - **Everything else:** IDF-weighted word overlap. Hard rejects: different outcome
     names, different numbers or years, conflicting words (top/last, above/below,
     reach/at, not, nominee/primary, ...). Soft penalties: far-apart close dates, and
     each side naming something the other doesn't. Only mutual-best pairs are kept.
   - Confidence tiers: **High ≥ 80%**, **Medium ≥ 60%**, **Low ≥ 40%**. A pair whose
     prices disagree by more than 15¢ is marked down, because that usually means the
     questions differ.
3. **Price** (`arb/scanner.py`, `arb/arbitrage.py`). Every matched Polymarket market
   is re-priced from the live CLOB (`POST /books`). Gamma's bestBid/bestAsk can lag.
   Pairs with a positive edge then get the full Kalshi order book, and the trade is
   sized by walking both books up to your budget.

## Fees (taker)

- **Kalshi:** `ceil_to_cent(0.07 × multiplier × C × p × (1−p))`, with the multiplier taken from each series.
- **Polymarket:** `feeRate × C × p × (1−p)`, with `feeRate` taken from each market's `feeSchedule` (0 when fees are disabled).

## Not handled yet / caveats

- **Resolution rules.** Always read both markets' rules: same headline does not mean same settlement
  (sources, deadlines, ties, cancellations).
- **Capital costs.** Capital is locked until resolution, and USDC/USD transfer costs are ignored.
- **Timing.** Kalshi top-of-book comes from the snapshot (≤ ~1 min old) unless the pair was depth-checked.
  Live games move faster than one scan.
- **Order rules.** Polymarket minimum order size (usually 5 shares) and tick sizes aren't enforced in sizing.
- **No trading.** The scanner never places orders.
