# Validation and limitations

What the replay backtests in this repo can and cannot support, what was checked after the competition, and what was found. Results are labeled **Observed** (measured on the 24 historical sessions), **Hypothesis** (an explanation not yet tested) or **Proposed test**.

## Three different numbers

Do not mix these.

| Measure | Definition | Used for |
|---|---|---|
| Official score | Each fill marked against the hidden true value 60 s later, news jumps in that window excluded | Leaderboard (R1 $1,480.18, R2 $35,876.94) |
| Replay proxy | Mid-price 120 batches after the fill batch, minus fees and penalties (`tools/sweep.py`) | Ranking strategies offline |
| GUI estimated PnL | Positions marked to microprice during the session | Live monitoring (R1 about $1,306, R2 about $25,154) |

The proxy is not the official score: it uses the observed mid, not the hidden value, and it does not exclude news jumps. The GUI figure is a mark-to-market estimate. The gap between R2's GUI estimate and the official score (about $25.2k vs $35.9k) is not explained by anything saved locally.

## Measurement fixes in `tools/sweep.py`

**Observed.** The first version had four issues, now fixed:

1. It stored the batch of the `on_batch` call that was running when the fill callback fired. Events for batch *b* are delivered before `on_batch(b)`, so that stored batch is the fill batch minus 1 in 100% of fills, and the horizon was 119 batches. It now uses `fill.batch`.
2. Marks past the end of the session were clamped to the last top. About 2.0% of shares were marked at a truncated horizon. They are now excluded.
3. Fills with a one-sided mark top (0.41%) were skipped for gross P&L but still charged fees. Fees and penalties now come from the same included fills, and the excluded share of volume is reported as `excl` (about 2.4% for `yl_touch`).
4. There is still no news adjustment, so the proxy includes news jumps that the official score removes.

`tests/test_sweep.py` checks the convention.

## What the replay cannot tell you

**Observed** from the SDK backtester and the data:

- Resting orders fill only when a recorded trade prints strictly through them. There is no queue position, no cancel latency and no partial-queue effects.
- Your orders never move the market, so size scaling in replay is capped only by recorded trade sizes. Real capacity against other teams is unknown.
- Historical median spreads are HPPO 5c, BNKY 3c, CRRT 5c, BRRY 11c. Live spreads were around 1 to 2 cents. Passive edge measured on wide-spread history does not transfer.
- Adverse selection by informed participants is absent.

So replay dollars rank strategies under one stylized fill model; they do not estimate live P&L. This is why R1, tuned on replay, was picked off live.

## Signal checks net of costs

All **Observed**, odd sessions train and even sessions test unless stated.

- The 60 s forecast is stable across the odd/even split and robust to a label lag. Against single-feature benchmarks, most of BRRY's signal is the gap between BRRY and the HPPO basket, not an independent factor.
- Passive fills sort strongly by agreement with the forecast at decision time (net c/share, forecast disagrees by more than 3c vs agrees by more than 3c): BNKY -1.83 vs +3.56, CRRT -1.34 vs +6.01, BRRY -0.60 vs +8.63. Correlations between edge and markout are 0.27, 0.34 and 0.44.
- Yet pulling the quote on the disfavored side loses money in replay. EMA-centred baseline quotes, corrected 60 s metric, 24 sessions:

| Variant | Net $/session | Shares/session | Net c/share | BRRY $/session |
|---|---|---|---|---|
| Baseline | 3,765 | 157k | 2.39 | 2,156 |
| Pull a side if \|forecast\| >= 6c | 3,485 | 149k | 2.34 | 1,879 |
| Pull if >= 4c | 2,961 | 134k | 2.21 | 1,367 |
| Pull if >= 2c | 2,231 | 103k | 2.17 | 803 |

  More pulling gives less money, and net cents per share also falls slightly. The removed fills were still profitable on average; only the extreme-disagreement fills were negative.
- Inventory skew is worth about $1.5k per session in replay (3,765 vs 2,260 with skew 0), by raising volume from 109k to 157k shares. That is a fill-model effect and should not be read as alpha.
- The HPPO taker added about +$296 per session at +3.09 net c/share, but its fills happened at median absolute exposure 211 (p95 370, max 458) vs 66 (p95 188, max 318) without it. The gain is small against the exposure it consumes.
- In `strategies/yl_alpha.py` the default `PQT_FILTER=999` puts quotes in "filter mode" centred on the slow EMA, where `alpha_pct` has no effect. Forecast-centred quoting needs `PQT_FILTER=0`. Confirm the mode when reading any forecast-vs-baseline comparison.

**Hypothesis.** In a no-queue replay each lost fill costs a full spread capture, so volume dominates quality. Live, queue position and adverse selection could change that trade-off. Replay cannot settle it.

## Proposed tests

1. Queue haircut: fill only a fraction of print-through volume, and check whether strategy rankings survive.
2. Cancel latency of 1 to 3 batches, and partial fills.
3. Frozen rules on a chronological holdout, with the final run done once.
4. A news-adjusted markout, to align the proxy with the official score.
5. HPPO taker size and exposure-cap curves with noise bands from session bootstrap.
