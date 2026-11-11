"""yl_combo = yl_news + yl_flow in one program for the algo2 key (R2).

PART 1 (yl_news): second team program (algo2 key). Trades ONLY in the window right after a news item,
in the direction the price is already moving, with IOC orders (nothing ever rests on the book).

Why: yl_mm (algo1) cancels all its quotes for news_pause batches after any news item, which is exactly
when prices move most. Scoring is the true value 60 s after each fill, and the true value is assumed to jump
at the news while traded prices take ~35 s (stocks) / ~8 s (HPPO) to catch up. A buy after good news,
while the price is still climbing, is scored against the new value.

Safe alongside yl_mm:
  * different client-id prefix ("zn"), so yl_mm never cancels or scores these orders;
  * IOC only: no resting orders, no order-count use, a few messages per news item;
  * trades only inside the news window, while yl_mm is paused (keep window <= yl_mm news_pause);
  * self-trade guard: never sends an IOC that would cross any resting order of ours;
  * shared position and exposure limits are checked before every order (exposure capped at 560 of 600).

Signal, per symbol, from news batch nb to nb + window:
  move = mid - mid at the news.  Fire when |move| >= threshold AND the price is still moving that way
  (at least 2 of the last 3 one-batch changes in that direction, none against).
  threshold = max(1.5 x current spread, MIN_MOVE[sym]).  An instant jump that is already complete
  does not fire (nothing left to capture); a gradual adjustment does.
At most `shots` orders per symbol per news item; a second shot only after the move extends by
another threshold beyond the first fill price.

    pqt run strategies/yl_news.py --mode live --server https://play.princeton-quant.com --cancel-on-disconnect
"""
from __future__ import annotations

import random
import string
from collections import deque

from pqt_client.v2 import BatchState, Side, Strategy

SYMS = ("HPPO", "BNKY", "CRRT", "BRRY")
WEIGHT = {"HPPO": 1.0, "BNKY": 0.15, "CRRT": -0.29, "BRRY": 0.425}
SIZE = {"HPPO": 100, "BNKY": 300, "CRRT": 400, "BRRY": 100}     # shares per shot at size_pct 100 (R1: CRRT fell 250 c in ~60 s on a headline)
MIN_MOVE = {"HPPO": 6, "BNKY": 3, "CRRT": 3, "BRRY": 6}         # cents; ~3x the 5 s diffusion sd
E_CAP = 560
BLOCKED = {"HPPO"}   # team decision in R2: never trade HPPO
PREFIX = "zn"


# PART 2 (yl_flow): HPPO follow. A 5 c move over 20 batches sets the target to +/-FLOW_Q in that direction.
FLOW = ("HPPO", 20, 5, 60, 2)     # symbol, lookback batches, min move c, target |position|, max spread c
FLOW_COOL = 20


class YLCombo(Strategy):
    # No MODEL_PARAMS on purpose: the team has ONE panel schema, and a second program declaring its own
    # could replace yl_mm's between sessions. This program reads the team panel instead:
    #   active      (yl_mm's) 0 stops this program too
    #   news_pause  (yl_mm's) the trading window ends 10 batches before yl_mm re-enters
    #   news2_pct   (optional, if yl_mm declares it) this program's size, 0 = off
    def P(self) -> dict:
        v = dict(self.model_params)
        pause = int(v.get("news_pause", 60))
        return dict(n_active=int(v.get("active", 1)), n_size_pct=int(v.get("news2_pct", 100)),
                    n_window=max(0, min(200, pause - 10)), n_shots=4, n_slip=1)

    def on_start(self) -> None:
        self.run_tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        self.seq = 0
        self.news_seen: set[int] = set()
        self.rejects: dict[str, int] = {}
        self.sessions = 0
        self.reset_session()
        self.log.warning("START %s mode, session %s, run %s, params %s", self.mode,
                         getattr(self.session, "session_id", "?"), self.run_tag, self.P())

    def reset_session(self) -> None:
        self.sessions = getattr(self, "sessions", 0) + 1
        self.batch = -1
        self.mids = {s: deque(maxlen=8) for s in SYMS}     # (batch, mid)
        self.events: list[dict] = []
        self.shots_total = 0
        self.fmids: deque = deque(maxlen=64)
        self.last_flow = -10**9

    # ---------------------------------------------------------------- events
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
        self.log.warning("END: %d shots, pos %s, rejects %s", self.shots_total,
                         {s: self.position(s) for s in SYMS}, self.rejects or "none")

    def on_news(self, item) -> None:
        if item.news_id in self.news_seen:
            return
        self.news_seen.add(item.news_id)
        nb = item.batch if item.batch is not None else self.batch
        if nb + self.P()["n_window"] <= self.batch:
            return  # old item (late start / replayed stack)
        # base = last mid at or before the news batch (prices start moving after it)
        base = {}
        for s in SYMS:
            before = [m for b, m in self.mids[s] if b <= nb]
            if before:
                base[s] = before[-1]
            elif self.mids[s]:
                base[s] = self.mids[s][0][1]
        self.events.append(dict(nb=nb, base=base, shots={s: 0 for s in SYMS}, last_px={}))
        self.log.warning("NEWS at batch %d: watching %d batches: %s", nb,
                         self.P()["n_window"], (item.headline or "")[:80])

    # ------------------------------------------------------------- per batch
    def on_batch(self, state: BatchState) -> None:
        if state.batch < self.batch - 5:
            self.reset_session()
            self.log.warning("NEW SESSION detected at batch %d", state.batch)
        self.batch = state.batch
        self.news_step(state)
        if not self.events and self.P()["n_active"]:     # the follow rule stands aside during news windows
            self.flow_step(state)

    def news_step(self, state: BatchState) -> None:
        for s in SYMS:
            t = state.top[s]
            if t.bid_px is not None and t.ask_px is not None and not t.halted:
                self.mids[s].append((state.batch, (t.bid_px + t.ask_px) / 2))
        for item in state.news_stack:
            if item.news_id not in self.news_seen:
                self.on_news(item)
        p = self.P()
        window = p["n_window"]
        self.events = [e for e in self.events if state.batch <= e["nb"] + window]
        if not self.events or not p["n_active"] or p["n_size_pct"] <= 0:
            return
        sent = 0
        for e in self.events:
            for s in SYMS:
                if sent >= 3:          # leave the team's 20-message budget alone
                    return
                if e["shots"][s] >= int(p["n_shots"]) or s not in e["base"]:
                    continue
                d = self.signal(state, s, e)
                if d and self.fire(state, s, d, e, p):
                    sent += 1

    def signal(self, state: BatchState, s: str, e: dict) -> int:
        t = state.top[s]
        if t.halted or t.bid_px is None or t.ask_px is None or len(self.mids[s]) < 4:
            return 0
        if self.mids[s][-1][0] != state.batch:
            return 0
        mid = self.mids[s][-1][1]
        thr = max(1.5 * (t.ask_px - t.bid_px), MIN_MOVE[s])
        ref = e["last_px"].get(s, e["base"][s])     # second shot must extend beyond the first fill price
        move = mid - ref
        if abs(move) < thr:
            return 0
        d = 1 if move > 0 else -1
        m = [x for _, x in self.mids[s]][-4:]
        steps = [m[i + 1] - m[i] for i in range(3)]
        with_ = sum(1 for x in steps if x * d > 0)
        against = sum(1 for x in steps if x * d < 0)
        return d if with_ >= 2 and against == 0 else 0

    def fire(self, state: BatchState, s: str, d: int, e: dict, p) -> bool:
        if s in BLOCKED:
            return False
        t = state.top[s]
        px = t.ask_px + int(p["n_slip"]) if d > 0 else t.bid_px - int(p["n_slip"])
        # self-trade guard: any resting order of the team that this IOC could hit
        for o in self.open_orders(s):
            if (d > 0 and o.side is Side.SELL and o.price <= px) or (d < 0 and o.side is Side.BUY and o.price >= px):
                self.log.warning("SKIP %s: a resting order of ours at %.2f would be crossed", s, o.price / 100)
                return False
        info = self.symbol_info[s]
        pos = self.position(s)
        room = info.position_limit - pos if d > 0 else info.position_limit + pos
        ex = sum(WEIGHT[x] * self.position(x) for x in SYMS)
        lu = state.limit_usage
        if lu is not None:     # worst case including the other program's resting orders
            ex = (lu.exposure_high if d * WEIGHT[s] > 0 else lu.exposure_low) / 1000
        e_room = (E_CAP - ex * (1 if d * WEIGHT[s] > 0 else -1)) / abs(WEIGHT[s])
        touch_qty = (t.ask_qty if d > 0 else t.bid_qty) or 0
        qty = int(max(0, min(SIZE[s] * p["n_size_pct"] / 100, info.max_order_qty, room - 1, e_room,
                             max(touch_qty, 1) * 2)))
        if qty <= 0:
            self.log.warning("SKIP %s: no room under the limits", s)
            return False
        self.seq += 1
        cid = f"{PREFIX}{self.run_tag}-{self.seq}"
        if d > 0:
            self.buy(s, qty, px, ioc=True, client_id=cid)
        else:
            self.sell(s, qty, px, ioc=True, client_id=cid)
        e["shots"][s] += 1
        e["last_px"][s] = px
        self.shots_total += 1
        self.log.warning("SHOT %s %s %d at %.2f, %d batches after news (base %.2f, touch %.2f/%.2f)", s,
                         "BUY" if d > 0 else "SELL", qty, px / 100, state.batch - e["nb"], e["base"][s] / 100,
                         t.bid_px / 100, t.ask_px / 100)
        return True

    # ----------------------------------------------------------- HPPO follow
    def flow_step(self, state: BatchState) -> None:
        s, look, k, q, max_sp = FLOW
        if s in BLOCKED:
            return
        t = state.top[s]
        if t.halted or t.bid_px is None or t.ask_px is None:
            self.fmids.clear()
            return
        mid = (t.bid_px + t.ask_px) / 2
        self.fmids.append((state.batch, mid))
        if state.batch - self.last_flow < FLOW_COOL or not self.fmids or self.fmids[0][0] > state.batch - look:
            return
        past = [m for b, m in self.fmids if b <= state.batch - look]
        if not past:
            return
        move = mid - past[-1]
        if abs(move) < k or t.ask_px - t.bid_px > max_sp:
            return
        d = 1 if move > 0 else -1
        pos = self.position(s)
        want = d * q - pos
        if want * d <= 0:
            return
        info = self.symbol_info[s]
        ex = sum(WEIGHT[x] * self.position(x) for x in SYMS)
        lu = state.limit_usage
        if lu is not None:
            ex = (lu.exposure_high if d > 0 else lu.exposure_low) / 1000
        e_room = (E_CAP - ex * d) / WEIGHT[s]
        touch = (t.ask_qty if d > 0 else t.bid_qty) or 0
        qty = int(max(0, min(abs(want), info.max_order_qty, e_room, info.position_limit - abs(pos) - 1, max(touch, 1))))
        px = t.ask_px if d > 0 else t.bid_px
        for o in self.open_orders(s):
            if (d > 0 and o.side is Side.SELL and o.price <= px) or (d < 0 and o.side is Side.BUY and o.price >= px):
                qty = 0
        if qty <= 0:
            return
        self.seq += 1
        cid = f"{PREFIX}{self.run_tag}-{self.seq}"
        if d > 0:
            self.buy(s, qty, px, ioc=True, client_id=cid)
        else:
            self.sell(s, qty, px, ioc=True, client_id=cid)
        self.last_flow = state.batch
        self.shots_total += 1
        self.log.warning("FLOW %s %s %d at %.2f (moved %+.1f c in %d batches, pos %d -> target %d)", s,
                         "BUY" if d > 0 else "SELL", qty, px / 100, move, look, pos, d * q)
