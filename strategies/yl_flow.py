"""yl_flow: taker program (algo2 key). The 'opposite' of yl_mm: it never rests quotes; it crosses the
spread with IOC orders when a recent move predicts the next 60 s (the score horizon).

Per symbol, from the 24 historical sessions (60 s mid change after a move over the last LOOK batches):
  HPPO  moves of >= 5 c over 10 s continued (+3.9 c)     -> FOLLOW
  CRRT  moves of >= 3 c over 10 s continued (+1.9 c)     -> FOLLOW
  BRRY  moves of >= 5 c over 5 s reversed (-2.3 c)       -> FADE
  BNKY  no signal                                        -> off
Crossing costs ~0.9 c a share in a 1 c market (half spread + 0.4 c taker fee).

Position rule: each symbol has a target of -Q, 0 or +Q. A signal sets the target to +/-Q and the program
trades toward it; it never trades without a signal (no unwinding trades that only pay the spread).
Cooldown COOL batches per symbol after each order. Exposure capped at 560 of 600, limits checked first.
No panel parameters (the team panel belongs to yl_mm). Stop with Ctrl+C.
"""
from __future__ import annotations

import random
import string
from collections import deque

from pqt_client.v2 import BatchState, Side, Strategy

# symbol: (mode +1 follow / -1 fade, lookback batches, min move c, max |position|, max spread c to trade)
# Live tradeable set: HPPO only. In the history, with spreads <= 2 c, HPPO-follow made +$34/session;
# CRRT-follow and BRRY-fade lost money once crossing costs were counted (kept here, switched off).
RULES = {"HPPO": (1, 20, 5, 60, 2)}
RULES_OFF = {"CRRT": (1, 20, 3, 150, 2), "BRRY": (-1, 10, 5, 40, 2)}
WEIGHT = {"HPPO": 1.0, "BNKY": 0.15, "CRRT": -0.29, "BRRY": 0.425}
COOL = 20
E_CAP = 560
PREFIX = "zf"


class YLFlow(Strategy):
    def on_start(self) -> None:
        self.run_tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        self.seq = 0
        self.rejects: dict[str, int] = {}
        self.reset_session()
        self.log.warning("START %s mode, session %s, run %s, rules %s", self.mode,
                         getattr(self.session, "session_id", "?"), self.run_tag, RULES)

    def reset_session(self) -> None:
        self.batch = -1
        self.mids = {s: deque(maxlen=64) for s in RULES}
        self.last_shot = {s: -10**9 for s in RULES}
        self.shots = 0

    def on_round_state(self, rs) -> None:
        if str(getattr(rs, "phase", "")).upper().endswith("ARMED") and self.batch > 0:
            self.reset_session()
            self.log.warning("NEW SESSION: state reset")
        self.log.warning("SESSION %s %s", getattr(rs, "phase", rs), getattr(rs, "text", ""))

    def on_reject(self, reject) -> None:
        reason = str(getattr(reject, "code", reject))
        n = self.rejects.get(reason, 0) + 1
        self.rejects[reason] = n
        if n <= 3 or n % 50 == 0:
            self.log.warning("REJECT #%d %s: %s", n, reason, reject)

    def on_fill(self, fill) -> None:
        if str(fill.client_id or "").startswith(PREFIX):
            self.log.warning("FILL %s %s %d at %.2f", fill.symbol, fill.side.name, fill.qty, fill.price / 100)

    def on_end(self, state) -> None:
        self.log.warning("END: %d orders, pos %s, rejects %s", self.shots,
                         {s: self.position(s) for s in WEIGHT}, self.rejects or "none")

    def on_batch(self, state: BatchState) -> None:
        if state.batch < self.batch - 5:
            self.reset_session()
            self.log.warning("NEW SESSION detected at batch %d", state.batch)
        self.batch = state.batch
        sent = 0
        for s, (mode, look, k, q, max_sp) in RULES.items():
            t = state.top[s]
            if t.halted or t.bid_px is None or t.ask_px is None:
                self.mids[s].clear()
                continue
            mid = (t.bid_px + t.ask_px) / 2
            self.mids[s].append((state.batch, mid))
            if sent >= 2 or state.batch - self.last_shot[s] < COOL:
                continue
            past = [m for b, m in self.mids[s] if b <= state.batch - look]
            if not past or self.mids[s][0][0] > state.batch - look:
                continue
            move = mid - past[-1]
            if abs(move) < k or t.ask_px - t.bid_px > max_sp:
                continue
            d = mode * (1 if move > 0 else -1)          # +1 buy, -1 sell
            target = d * q
            pos = self.position(s)
            want = target - pos
            if want * d <= 0:
                continue                                  # already at/over the target that way
            info = self.symbol_info[s]
            ex = sum(WEIGHT[x] * self.position(x) for x in WEIGHT)
            lu = state.limit_usage
            if lu is not None:
                ex = (lu.exposure_high if d * WEIGHT[s] > 0 else lu.exposure_low) / 1000
            e_room = (E_CAP - ex * (1 if d * WEIGHT[s] > 0 else -1)) / abs(WEIGHT[s])
            touch = (t.ask_qty if d > 0 else t.bid_qty) or 0
            qty = int(max(0, min(abs(want), info.max_order_qty, e_room, info.position_limit - abs(pos) - 1,
                                 max(touch, 1))))
            if qty <= 0:
                continue
            for o in self.open_orders(s):   # never hit a resting order of our own team
                if (d > 0 and o.side is Side.SELL and o.price <= t.ask_px) or (d < 0 and o.side is Side.BUY and o.price >= t.bid_px):
                    qty = 0
            if qty <= 0:
                continue
            self.seq += 1
            cid = f"{PREFIX}{self.run_tag}-{self.seq}"
            if d > 0:
                self.buy(s, qty, t.ask_px, ioc=True, client_id=cid)
            else:
                self.sell(s, qty, t.bid_px, ioc=True, client_id=cid)
            self.last_shot[s] = state.batch
            self.shots += 1
            sent += 1
            self.log.warning("SHOT %s %s %d at %.2f (%s: moved %+.1f c in %d batches, pos %d -> target %d)", s,
                             "BUY" if d > 0 else "SELL", qty, (t.ask_px if d > 0 else t.bid_px) / 100,
                             "follow" if mode > 0 else "fade", move, look, pos, target)
