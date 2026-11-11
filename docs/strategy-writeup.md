# Strategy writeup: four-symbol market maker

> **Pre-competition snapshot, written before the Nov 7 competition.** Numbers below are replay backtests on the 24 historical sessions with a 60 s mid-markout proxy, and describe an early version of `strategies/yl_mm.py`. The final live version changed (faster fair value, anti-sniping switch, news-view panel). For what actually happened in the sandbox and live rounds, see [live-and-sandbox-results.md](live-and-sandbox-results.md).

Our 3-person student team's analysis for the Princeton Quantitative Traders (PQT) intercollegiate algorithmic trading competition: market rules, scoring, strategy, backtests, stress tests, and what we tried that did not work.

Starter kit (rules, SDK, examples, data): https://github.com/tom05919/pqt-student-kit

---

## 1. The market

- Four symbols, prices in whole cents, simulated exchange:

| symbol | what it is | position limit (± shares) | max order | exposure weight | typical price | median spread |
|---|---|---|---|---|---|---|
| HPPO | ETF = 1 BNKY + 2 BRRY (true values exactly) | 1,000 | 200 | 1.00 | ~$180 | 4.8¢ |
| BNKY | stock | 2,500 | 500 | 0.15 | ~$105 | 3.4¢ |
| CRRT | stock | 2,500 | 500 | −0.29 | — | 4.8¢ |
| BRRY | stock | 600 | 120 | 0.425 | ~$37 | 11.2¢ |

- **Net exposure** = Σ weight × position must stay within **±600** (the exchange checks it as if every resting order filled).
- **Batches**: the exchange collects all messages for 500 ms and processes them together (random participant order each batch), so sub-batch latency is worthless. A session = 5,400 batches = 45 min, split into five 8-minute "trading days" + 5 min.
- **Limits**: 20 messages per batch per team (shared by the program and teammates' manual GUI orders).
- **Fees**: taker $0.004/share, maker rebate $0.002/share, HPPO create/redeem conversion $0.02/share, self-trade penalty $0.10/share.
- **Other participants**: hidden bots (not published who/how). Market parameters are redrawn each session from the same distribution as the 24 historical sessions.
- **News**: free-text headlines/bodies during the session; no symbol tag or direction given — you have to read it. Halts are possible.
- **Everyone starts flat**, no cash limit, short selling allowed.
- **Rule conflict we had not resolved when this was written**: the repo guide says all 60–70 teams share ONE market; the newer participant guide (Nov 5 PDF) says "each team uses its own simulation environment… individual simulated markets under equivalent underlying conditions, with a shared leaderboard." We'll ask at the briefing.

## 2. Scoring (this is the key design driver)

- Each **fill** is marked against the hidden **true value V 60 seconds later**: buy at P, later value V₆₀ → score q·(V₆₀ − P). Plus/minus fees and penalties.
- **News adjustment**: if a news item is published after your fill and within its 60 s, that news jump is removed from your markout (you're not credited/charged for news you couldn't know). Fills *after* news at stale prices ARE charged.
- Holding inventory is not scored directly: there is no end-of-session mark-to-market in the score. A directional bet held for minutes has ~zero expected score. Profit comes from buying below value / selling above value, avoiding fills the market then runs over, reading news, and HPPO-vs-basket arbitrage.
- **Final score = R1 + 2 × R2** (two 45-min sessions; R2 counts double). Tie-break: realized PnL.

## 3. Data we have

24 historical 45-min sessions (top of book per symbol per batch + all trades with aggressor side). No news, no halts, no other teams' orders. Our own orders never move the recorded market (replay only).

The SDK's backtester fills a resting order only when a recorded trade prints **strictly through** its price (queue position unknown), each recorded trade filling at most its own size across our orders. Marketable orders fill at the recorded touch.

**Our offline score proxy**: since V is hidden, we mark each backtest fill against the **mid 120 batches (60 s) later**, minus fees. We optimize this, not the backtester's PnL.

## 4. Our strategy: `yl_mm.py` (four-symbol market maker)

Per symbol, every batch:
- fair = EMA of mid, alpha 1/30 per batch (~15 s)
- centre = fair − skew × position / cap   (inventory lean)
- bid = floor(centre − half), ask = ceil(centre + half), clamped to not cross the book (bid ≤ ask_touch − 1, ask ≥ bid_touch + 1)
- one bid + one ask resting per symbol, cancel/replace only when the price changes
- stop quoting the side that would add to |net exposure| above 420 (hard limit 600); per-symbol position caps

| symbol | half-spread | size | skew at cap | cap |
|---|---|---|---|---|
| HPPO | 3¢ | 30 | 6¢ | 150 |
| BNKY | 2¢ | 50 | 3¢ | 500 |
| CRRT | 2¢ | 50 | 3¢ | 500 |
| BRRY | 4¢ | 60 | 6¢ | 300 |

- **News handler**: regex for symbol names in headline+body. News naming BNKY or BRRY also pauses HPPO (it's their basket); news naming HPPO pauses BNKY+BRRY; news naming no symbol pauses all. Paused = cancel quotes for `news_pause` batches (default 60 = 30 s), measured from the news item's batch.
- **Live controls** (model params a teammate changes from the trader GUI mid-session without restarting): `active` (0 = cancel everything), `size_pct` (global size), per-symbol `hppo_pct`/`bnky_pct`/`crrt_pct`/`brry_pct` (0 = off), `widen`, `news_pause`, `adapt`.
- **Monitoring**: every 2 minutes it logs each symbol's recent 60 s markout (cents/share, ~3-min half-life) computed from our own fills — the same quantity the exchange scores. Team rule: cut a symbol to 50% after 2 negative readings in a row, 0 after 3.
- Logs warnings on rejects, session-state changes, organizer broadcasts, reconnects, end-of-session summary.

## 5. Results (all on the 24 historical sessions, 60 s mid-markout proxy, $ per 45-min session)

**Headline**: mean **$4,105**/session, sd $888, **0 losing sessions**, worst $2,493, median $4,338, 0 rejects, peak |net exposure| 443. The kit's example single-symbol market maker (BNKY only): mean $241, 6 losing sessions.

**By symbol and under worse fills** (wider quotes as a stand-in for losing queue priority to competitors):

| symbol | normal | quotes 1¢ worse | 2¢ worse |
|---|---|---|---|
| BRRY | $2,082 | $2,110 | $1,885 |
| CRRT | $1,177 | $488 | $136 |
| BNKY | $271 | $12 | −$30 |
| HPPO | $211 | $23 | −$71 |
| total | $4,105 | $2,828 | $2,037 |

→ BRRY's wide spread is the robust edge; BNKY/HPPO only pay when we're first in queue.

**Size scaling** (`size_pct`): 50 → $2,300; 100 → $4,105; 150 → $5,290; 200 → $6,083 (no rejects, exposure still < 450). Concave. Since the backtester caps fills by each recorded trade's size, extra size only captures flow that actually printed — real capacity if our market is private, partly illusory if 60 teams compete for the same flow.

**Parameter search** (tuned on odd sessions, checked on even): quoting all 4 symbols was the biggest gain; faster fair EMA (30 vs 60 batches) helped every symbol; CRRT half-spread 2¢ vs 3¢ roughly doubled CRRT. All held out-of-sample.

**Trading-day structure**: spreads, volume, 60 s volatility, depth flat across the five 8-min days and the final 5 min; no exploitable boundary effect.

## 6. Stress tests (synthetic)

Built from 8 recorded sessions: spliced in three news-driven jumps (BRRY +J with HPPO +2J; BNKY −J with HPPO −J; unnamed-symbol news with CRRT +J) plus a 30 s BRRY halt. Scored with the exchange's news adjustment. Score per session, J = $1.00:

| price reaches new level | pause 10 s | pause 30 s (chosen) | no pause | jump with no news item |
|---|---|---|---|---|
| instantly | $3,821 | $3,792 | $3,803 | $3,683 |
| over 10 s | $3,870 | $3,855 | $3,857 | $3,729 |
| over 30 s | $3,771 | $3,849 | $3,497 | $3,418 |

No news at all: pause-10 $3,873, pause-30 $3,804 (pausing costs ~$20–25 per news item when news doesn't matter). Fake-news injection test confirms quotes are pulled on the right symbols and come back. Halts handled without crashes.

## 7. Things we tried that did NOT make it in

1. **Regression forecast of 60 s mid change** (features: mid − EMA60, mid − EMA240, 20-batch return, 60-batch signed trade flow, HPPO's 20-batch return, basket gap HPPO − BNKY − 2·BRRY). Train odd / test even, out-of-sample R²: **BRRY 0.23** (catches up to the basket gap and HPPO's moves), **HPPO 0.10** (momentum + flow continuation), CRRT 0.09 (moves opposite HPPO, lagged), BNKY 0.03. Positive predictive correlation on every held-out session. Takeaway: prices trend over ~1 min and HPPO leads.
   - **Shifting passive quotes toward the forecast made it WORSE** (−$500 to −$1,800/session). Using it only to pull the toxic side also hurt. Quoting around the lagging EMA beat quoting around the forecast in this backtest — we don't fully understand why (fill-model artifact? short-horizon bounce?).
   - **Crossing the spread on HPPO when forecast − half-spread > 2¢** earned +5–6¢/share after fees out-of-sample, hit rate 56–63%. But HPPO has exposure weight 1.0, so its position eats the exposure budget from the passive books; and passive HPPO quotes traded against the taker's inventory. HPPO taker-only + market-make the other three: total roughly $4,130 vs $4,105 — within noise. Not used live.
2. **Lead–lag between symbols at 1-batch horizon**: no signal (all |corr| < 0.01).
3. **Automatic rotation** (scale each symbol's size ×0.25–×1.5 by its rolling 60 s markout from our own fills): +11–20% in backtest, but ALL of the gain came from upsizing winners; cutting losers alone made it slightly worse in every scenario (per-symbol markout too noisy within a session). Kept as a manual rule + log readout instead; `adapt` off.
4. **Wider quotes** lost money on every symbol except BRRY.
5. Not tried: HPPO create/redeem basket arbitrage (kit has an example `etf_basket.py`), news *direction* trading (only defensive pausing), multiple price levels per side.

