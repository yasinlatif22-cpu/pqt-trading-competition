# Round 2 postmortem

Result: official R2 score **+$35,876.94** (R1 +$1,480.18; total score = R1 + 2 x R2 = $73,234.06, 3rd place).

## What ran

- Maker: `yl_touch` (quote at the touch, 5-batch fair value, longer news pause, inventory skew, 2-minute monitor).
- Takers: `yl_combo` (news and flow), then the larger-size `yl_aggr` (BNKY, CRRT, BRRY only).
- Manual orders from the GUI (`gui.*` client ids), many rejected by exposure and rate limits.
- Team-wide shared position, exposure and message budgets.

## What is measured

| Item | Value | Source |
|---|---|---|
| Official R2 score | $35,876.94 | Leaderboard |
| GUI estimated PnL at session end | about $25,154 | GUI screenshot |
| Fees net of rebates (GUI) | $358.744 | GUI screenshot |
| Self-trade penalties | $0.000 | GUI screenshot |

The GUI figure marks to microprice; the official score marks to the hidden true value 60 s after each fill. Different measures, so a gap is expected. Size and cause of the gap cannot be decomposed from what was saved.

## What cannot be attributed

The saved records do not support a fill-by-fill split of R2 by maker, news taker, flow taker and manual orders:

- Logs cover roughly 15:13 to 15:33 of a session that ran 15:20 to 16:05, and have no end-of-session summary.
- There is no market tape, so post-fill markouts cannot be recomputed.
- Maker logs contain monitor summaries, not individual fills; taker logs have FILL and SHOT lines without batch numbers.

Treat any claim like "the takers earned X of the $35.9k" as unsupported.

## Exhibits to build when the data exists

1. Fill-level table: component, symbol, seconds since news, spread captured, 60 s markout, fee, exposure at fill.
2. Replay vs live: fill rate and signed markout by symbol, with sensitivity bands (queue haircut, cancel latency).
3. Signal vs cost: forecast agreement against net markout, and the pull-threshold table in [validation-and-limitations.md](validation-and-limitations.md).
4. Risk: exposure-at-fill distributions and the HPPO taker trade-off.
5. Replay ablations (rank changes, not dollars): quote placement, fair-value speed, news pause, taker logic on or off, sizing.

## What to log next time

Every fill with batch, client id and liquidity flag, a top-of-book snapshot per batch, every news item with its timestamp, and rejects with reasons, written to disk from the first batch.
