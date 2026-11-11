"""Four-symbol market maker for the PQT competition (v3: fast fair value + live anti-sniping switch).

Quotes one bid and one ask on HPPO, BNKY, CRRT and BRRY around a slow average of each mid,
leans against inventory, keeps net exposure away from the 600 limit, steps back after news,
and re-anchors its fair value from fresh prices before quoting again.

    pqt backtest strategies/yl_mm.py --data data --name s01
    pqt run strategies/yl_mm.py --mode test --server https://sandbox.princeton-quant.com --cancel-on-disconnect

Live controls (trader GUI model panel):
    active      1 = quote, 0 = cancel all of OUR orders and stand aside
    size_pct    scales every quote size (100 = the sizes in CFG)
    hppo_pct, bnky_pct, crrt_pct, brry_pct   per-symbol size; 0 = stop quoting that symbol
    widen       extra cents on every half-spread (leave at 0: widening lost money in tests)
    news_pause  batches to stand aside after news (60 = 30 s)
    fair_lag    batches in the fair-value average (5 = 2.5 s). v2 used 30; in the sandbox the lag got
                picked off. Lower = follows the market faster
    news_all    1 = any news item pauses all four symbols (default); 0 = only the named ones (+ basket links)
    view_hppo, view_bnky, view_crrt, view_brry
                YOUR READ OF THE NEWS: +1 = price going up, -1 = going down, 0 = no view (default).
                Changing a symbol from 0 to +1/-1 fires ONE immediate order: buy (or sell) view_qty
                shares, crossing the spread by up to view_slip cents. While the view is on, the program
                never quotes against it (no asks while +1, no bids while -1). Set it back to 0 after
                about a minute; to fire again, go to 0 first and then +1/-1. The position is simply
                held: holding is never scored, only the price you got versus the value 60 s later.
    join_touch  1 = never quote better than the current best bid/ask (safer against bots that pick
                off stale quotes; earns about half as much in replay). Switch on if BNKY/CRRT go ADVERSE

Order ownership: the program only touches orders whose client_id starts with "yl". A teammate's
manual order from the GUI is never cancelled or replaced by it (but still counts toward the team's
limits and message budget, so avoid manual orders while it runs).

Monitoring (WARNING lines in the log):
    BLOCK ... every 2 minutes per symbol, for the fills made in that 2-minute block once they are
    60 s old: shares, NET mid-markout proxy in cents/share after fees, our share of market volume,
    and a RECOMMEND with the panel change to make (or HOLD). The proxy uses the mid, not the hidden
    true value, and leaves out fills whose 60 s window contains news or a missing price.
"""

import math
import random
import re
import string
from collections import deque

from pqt_client.v2 import BatchState, ParamKind, ParamSpec, Side, Strategy

E_SOFT = 420               # above this |net exposure|, stop quoting the side that adds to it
SETTLE = 4                 # two-sided batches needed before re-anchoring fair after a pause/halt/gap
ONE_SIDED_STALE = 4        # one-sided batches before we pull our quotes and re-anchor later
REENTRY = 20               # batches at half size after re-anchoring
REFILL_FRACTION = 3        # top a partly filled order back up only when it is below size / 3

H = 120                    # markout horizon: 60 s
BLOCK = 240                # monitor block: 2 minutes
MIN_SH = 150               # clean shares in a block before it counts as evidence
ADVERSE_CPS = -0.25        # net c/share in two consecutive blocks -> cut
HEALTHY_CPS = 1.0          # net c/share in two consecutive blocks -> may raise
STEPS = (0, 25, 50, 100, 150, 200)

#            half-spread (c), size, skew (c at cap), cap (shares)
CFG = {
    "HPPO": dict(half=3, size=30, skew=6, cap=150),
    "BNKY": dict(half=2, size=50, skew=3, cap=500),
    "CRRT": dict(half=2, size=50, skew=3, cap=500),
    "BRRY": dict(half=4, size=60, skew=6, cap=300),
}
WEIGHT = {"HPPO": 1.0, "BNKY": 0.15, "CRRT": -0.29, "BRRY": 0.425}
PREFIX = "yl"


def step(pct: int, direction: int) -> int:
    lower = [s for s in STEPS if s < pct]
    higher = [s for s in STEPS if s > pct]
    if direction < 0:
        return max(lower[-1] if lower else 0, 25 if pct > 25 else 0)
    return higher[0] if higher else pct


class YLMarketMaker(Strategy):
    MODEL_PARAMS = (
        ParamSpec("size_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("widen", ParamKind.INT, 0, 0, 20),
        ParamSpec("news_pause", ParamKind.INT, 60, 0, 240),
        ParamSpec("active", ParamKind.INT, 1, 0, 1),
        ParamSpec("hppo_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("bnky_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("crrt_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("brry_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("fair_lag", ParamKind.INT, 5, 1, 60),
        ParamSpec("join_touch", ParamKind.INT, 0, 0, 1),
        ParamSpec("news_all", ParamKind.INT, 1, 0, 1),
        ParamSpec("view_hppo", ParamKind.INT, 0, -1, 1),
        ParamSpec("view_bnky", ParamKind.INT, 0, -1, 1),
        ParamSpec("view_crrt", ParamKind.INT, 0, -1, 1),
        ParamSpec("view_brry", ParamKind.INT, 0, -1, 1),
        ParamSpec("view_qty", ParamKind.INT, 50, 0, 200),
        ParamSpec("view_slip", ParamKind.INT, 2, 0, 20),
    )

    # ------------------------------------------------------------------ setup
    def on_start(self) -> None:
        self.run_tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        self.seq = 0
        self.rejects: dict[str, int] = {}
        self.news_seen: set[int] = set()
        self.sessions = 0
        self.last_view = {s: 0 for s in CFG}
        self.shot_batch: dict[str, int] = {}
        self.reset_session()
        self.log.warning("START %s mode, session %s, %s batches, run %s, params %s", self.mode,
                         getattr(self.session, "session_id", "?"), getattr(self.session, "n_batches", "?"),
                         self.run_tag, dict(self.model_params))

    def reset_session(self) -> None:
        """Per-session state. A live program can stay connected across sessions (on_start runs once),
        so this also runs whenever a new session begins: batch numbers restart and every team starts flat."""
        self.sessions = getattr(self, "sessions", 0) + 1
        self.batch = -1
        self.fair: dict[str, float] = {}
        self.stale = {s: True for s in CFG}          # fair must be (re)anchored before quoting
        self.settle = {s: 0 for s in CFG}            # consecutive two-sided batches seen
        self.one_sided = {s: 0 for s in CFG}
        self.reentry_until = {s: 0 for s in CFG}
        self.paused_until = {s: 0 for s in CFG}
        self.news_events: list[tuple[int, set]] = []  # (batch, symbols affected)
        self.last_mid: dict[str, float] = {}
        self.pending: deque = deque()                # fills waiting 60 s
        self.blocks = {s: {} for s in CFG}           # block -> stats
        self.done_blocks = {s: [] for s in CFG}      # completed block stats, in order
        self.base = {s: None for s in CFG}           # (participation, market shares) baseline
        self.cooldown = {s: -1 for s in CFG}         # block index until which no new change is suggested
        self.worst_e = 0.0

    # --------------------------------------------------------------- events
    def on_reject(self, reject) -> None:
        reason = str(getattr(reject, "code", reject))
        n = self.rejects.get(reason, 0) + 1
        self.rejects[reason] = n
        if n <= 3 or n % 100 == 0:
            self.log.warning("REJECT #%d %s: %s", n, reason, reject)

    def on_round_state(self, rs) -> None:
        if str(getattr(rs, "phase", "")).upper().endswith("ARMED") and self.batch > 0:
            self.reset_session()
            self.log.warning("NEW SESSION: state reset (session %d of this run)", self.sessions)
        self.log.warning("SESSION %s (batch %s of %s) %s", getattr(rs, "phase", rs),
                         getattr(rs, "batch", "?"), getattr(rs, "n_batches", "?"), getattr(rs, "text", ""))

    def on_broadcast(self, b) -> None:
        self.log.warning("ORGANISERS: %s", getattr(b, "text", b))

    def on_reconnect(self) -> None:
        for s in CFG:
            self.mark_stale(s)
        self.log.warning("RECONNECTED at batch %d: resting orders kept; fair values will re-anchor", self.batch)

    def on_end(self, state) -> None:
        self.log.warning("END: pos %s, est PnL $%.2f (display only), worst-case exposure %.0f, rejects %s",
                         {s: self.position(s) for s in CFG}, state.pnl_est_mills / 1000, self.worst_e,
                         self.rejects or "none")

    def on_fill(self, fill) -> None:
        if not str(fill.client_id or "").startswith(PREFIX):
            return  # a teammate's manual order: not ours to score
        sign = 1 if fill.side is Side.BUY else -1
        cost_c = (fill.fee_mills + fill.penalty_mills) / 10.0   # mills -> cents (rebate is negative)
        blk = fill.batch // BLOCK
        st = self.block_stats(fill.symbol, blk)
        st["sh"] += fill.qty
        st["pct"].add(self.current_pct(fill.symbol))
        self.pending.append((fill.batch, fill.symbol, sign, fill.price, fill.qty, cost_c, blk))

    # ------------------------------------------------------------------ news
    def on_news(self, item) -> None:
        if item.news_id in self.news_seen:
            return
        self.news_seen.add(item.news_id)
        text = f"{item.headline} {item.body}".upper()
        named = set(re.findall(r"\b(HPPO|BNKY|CRRT|BRRY)\b", text))
        # HPPO = BNKY + 2 BRRY: news on a component moves the ETF, and news on the ETF moves both
        if "HPPO" in named:
            named |= {"BNKY", "BRRY"}
        elif named & {"BNKY", "BRRY"}:
            named.add("HPPO")
        # news_all=1: any item pauses every symbol. A jump in the HPPO family moves CRRT too
        # (exposure weight -0.29, 60 s beta about -0.2), and the reverse, so stale quotes anywhere get picked off.
        hit = set(CFG) if (self.model_params["news_all"] or not named) else set(named)
        self.news_events.append((item.batch, hit))
        until = item.batch + int(self.model_params["news_pause"])
        if until <= self.batch:
            return  # an old item (late start / replayed stack): nothing to step aside from
        for s in hit:
            self.paused_until[s] = max(self.paused_until[s], until)
            self.cancel_mine(s)
            self.mark_stale(s)
        self.log.warning("NEWS at batch %d, pausing %s until %d: %s", item.batch, sorted(named) or "ALL",
                         until, item.headline[:80])

    # ------------------------------------------------------------- per batch
    def on_batch(self, state: BatchState) -> None:
        if state.batch < self.batch - 5:   # batch numbers restarted: a new session we were not told about
            self.reset_session()
            self.log.warning("NEW SESSION detected at batch %d: state reset (session %d of this run)",
                             state.batch, self.sessions)
        self.batch = state.batch
        # news the SDK did not hand to on_news (e.g. published while we were disconnected)
        for item in state.news_stack:
            if item.news_id not in self.news_seen:
                self.on_news(item)
        lu = state.limit_usage
        if lu is not None:
            self.worst_e = max(self.worst_e, abs(lu.exposure_low) / 1000, abs(lu.exposure_high) / 1000)
        self.monitor(state)
        p = self.model_params
        self.news_views(state, p)
        e = self.exposure()
        for sym, c in CFG.items():
            self.quote(state, sym, c, e, p)

    def news_views(self, state: BatchState, p) -> None:
        """A teammate's read of the news, from the panel: fire one order when a view switches on."""
        for sym in CFG:
            v = int(p[f"view_{sym.lower()}"])
            if v == self.last_view.get(sym, 0):
                continue
            self.last_view[sym] = v
            if v == 0:
                self.log.warning("VIEW %s cleared", sym)
                continue
            top = state.top[sym]
            if top.halted or top.bid_px is None or top.ask_px is None or not p["active"]:
                self.log.warning("VIEW %s %+d: no trade (halted, one-sided or inactive)", sym, v)
                continue
            info = self.symbol_info[sym]
            pos = self.position(sym)
            room = info.position_limit - pos if v > 0 else info.position_limit + pos
            e, w = self.exposure(), WEIGHT[sym]
            e_room = (580 - e * (1 if v * w > 0 else -1)) / abs(w)
            qty = int(max(0, min(p["view_qty"], info.max_order_qty, room - 1, e_room)))
            if qty <= 0:
                self.log.warning("VIEW %s %+d: no room under the limits", sym, v)
                continue
            self.cancel_mine(sym)             # processed before the order below: no self-trade
            self.shot_batch[sym] = state.batch
            if v > 0:
                px = top.ask_px + int(p["view_slip"])
                self.buy(sym, qty, px, ioc=True, client_id=self.new_cid())
            else:
                px = top.bid_px - int(p["view_slip"])
                self.sell(sym, qty, px, ioc=True, client_id=self.new_cid())
            self.log.warning("VIEW %s %+d: %s %d at %.2f (touch %.2f/%.2f)", sym, v, "BUY" if v > 0 else "SELL",
                             qty, px / 100, top.bid_px / 100, top.ask_px / 100)

    def exposure(self) -> float:
        return sum(WEIGHT[s] * self.position(s) for s in CFG)

    def current_pct(self, sym: str) -> int:
        p = self.model_params
        if not p["active"]:
            return 0
        return int(p["size_pct"] * p[f"{sym.lower()}_pct"] / 100)

    def mark_stale(self, s: str) -> None:
        self.stale[s] = True
        self.settle[s] = 0

    def quote(self, state: BatchState, sym: str, c: dict, e: float, p) -> None:
        # 1. controls first: they must work even when the book is empty or halted
        pct = self.current_pct(sym)
        if pct <= 0:
            self.cancel_mine(sym)
            return
        top = state.top[sym]
        two_sided = (not top.halted) and top.bid_px is not None and top.ask_px is not None
        # 2. market state: halts, empty sides, news pause -> pull and re-anchor later
        if not two_sided:
            self.settle[sym] = 0
            self.one_sided[sym] += 1
            if top.halted:
                self.mark_stale(sym)       # the exchange cancels resting orders itself on a halt
            elif self.one_sided[sym] >= ONE_SIDED_STALE:
                self.mark_stale(sym)
                self.cancel_mine(sym)
            return
        self.one_sided[sym] = 0
        mid = (top.bid_px + top.ask_px) / 2
        self.last_mid[sym] = mid
        f = self.fair.get(sym)
        self.fair[sym] = mid if f is None else f + (mid - f) / max(1, int(p["fair_lag"]))
        if state.batch < self.paused_until[sym] or self.shot_batch.get(sym) == state.batch:
            return
        if self.stale[sym]:
            self.settle[sym] += 1
            if self.settle[sym] < SETTLE:
                return
            self.fair[sym] = mid                       # re-anchor on fresh, settled prices
            self.stale[sym] = False
            self.reentry_until[sym] = state.batch + REENTRY
        # 3. prices and sizes
        pos = self.position(sym)
        half = c["half"] + int(p["widen"])
        centre = self.fair[sym] - c["skew"] * pos / c["cap"]
        if p["join_touch"]:   # never better than the best bid/ask: stale quotes cannot lead the book
            bid = min(math.floor(centre - half), top.bid_px)
            ask = max(math.ceil(centre + half), top.ask_px)
        else:
            bid = min(math.floor(centre - half), top.ask_px - 1)
            ask = max(math.ceil(centre + half), top.bid_px + 1)
        size = c["size"] * pct / 100
        if state.batch < self.reentry_until[sym]:
            size /= 2
        size = max(1, min(int(size), self.symbol_info[sym].max_order_qty))
        w = WEIGHT[sym]
        buy_ok = pos + size <= c["cap"] and not (e > E_SOFT and w > 0) and not (e < -E_SOFT and w < 0)
        sell_ok = pos - size >= -c["cap"] and not (e < -E_SOFT and w > 0) and not (e > E_SOFT and w < 0)
        view = int(p[f"view_{sym.lower()}"])   # a teammate's news view: never quote against it
        if view > 0:
            sell_ok = False
        elif view < 0:
            buy_ok = False
        self.keep(sym, Side.BUY, bid if buy_ok else None, size)
        self.keep(sym, Side.SELL, ask if sell_ok else None, size)

    # ---------------------------------------------------------------- orders
    def mine(self, sym: str, side: Side | None = None):
        return [o for o in self.open_orders(sym)
                if str(o.client_id or "").startswith(PREFIX) and (side is None or o.side is side)]

    def cancel_mine(self, sym: str) -> None:
        for o in self.mine(sym):
            self.cancel(order_id=o.order_id)

    def new_cid(self) -> str:
        self.seq += 1
        return f"{PREFIX}{self.run_tag}-{self.seq}"

    def keep(self, sym: str, side: Side, price: int | None, size: int) -> None:
        mine = self.mine(sym, side)
        for extra in mine[1:]:
            self.cancel(order_id=extra.order_id)
        o = mine[0] if mine else None
        if price is None:
            if o:
                self.cancel(order_id=o.order_id)
        elif o is None:
            (self.buy if side is Side.BUY else self.sell)(sym, size, price, client_id=self.new_cid())
        elif o.price != price:
            self.replace(order_id=o.order_id, price=price, qty=size)
        elif o.remaining > size:
            self.replace(order_id=o.order_id, price=price, qty=size)   # same-price cut keeps priority
        elif o.remaining * REFILL_FRACTION < size:
            self.replace(order_id=o.order_id, price=price, qty=size)   # mostly filled: top up

    # --------------------------------------------------------------- monitor
    def block_stats(self, s: str, blk: int) -> dict:
        b = self.blocks[s].get(blk)
        if b is None:
            b = self.blocks[s][blk] = dict(sh=0, clean=0, net_c=0.0, news=0, stale=0, mkt=0, pct=set())
        return b

    def monitor(self, state: BatchState) -> None:
        blk_now = state.batch // BLOCK
        for t in state.trades:
            if t.symbol in CFG:
                self.block_stats(t.symbol, blk_now)["mkt"] += t.qty
        # mature fills 60 s old
        while self.pending and self.pending[0][0] + H <= state.batch:
            fb, s, sign, px, qty, cost_c, blk = self.pending.popleft()
            st = self.block_stats(s, blk)
            top = state.top[s]
            if any(fb < nb <= fb + H and s in syms for nb, syms in self.news_events):
                st["news"] += qty
                continue
            if top.halted or top.bid_px is None or top.ask_px is None:
                st["stale"] += qty
                continue
            ref = (top.bid_px + top.ask_px) / 2
            st["clean"] += qty
            st["net_c"] += sign * qty * (ref - px) - cost_c
        # a block is complete once its last fill is 60 s old
        done = blk_now - 1 if state.batch % BLOCK >= H else blk_now - 2
        for s in CFG:
            for blk in sorted(b for b in self.blocks[s] if b <= done):
                st = self.blocks[s].pop(blk)
                self.done_blocks[s].append((blk, st))
                self.report(s, blk, st)

    def report(self, s: str, blk: int, st: dict) -> None:
        cps = st["net_c"] / st["clean"] if st["clean"] else None
        part = st["sh"] / st["mkt"] if st["mkt"] else 0.0
        pct_now = self.model_params[f"{s.lower()}_pct"]
        hist = [x for _, x in self.done_blocks[s] if x["clean"] >= MIN_SH]
        if self.base[s] is None and len(hist) >= 2:
            self.base[s] = (sum(x["sh"] / max(1, x["mkt"]) for x in hist[:2]) / 2,
                            sum(x["mkt"] for x in hist[:2]) / 2)
        mode, rec = "HOLD", None
        prev = self.done_blocks[s][-2][1] if len(self.done_blocks[s]) >= 2 else None
        prev_cps = prev["net_c"] / prev["clean"] if prev and prev["clean"] >= MIN_SH else None
        if st["clean"] < MIN_SH:
            mode = "THIN"
        elif cps < ADVERSE_CPS and prev_cps is not None and prev_cps < ADVERSE_CPS:
            mode = "ADVERSE"
            if pct_now > 25:
                rec = step(pct_now, -1)
        elif cps >= HEALTHY_CPS and prev_cps is not None and prev_cps >= HEALTHY_CPS:
            mode = "HEALTHY"
            if pct_now < 200 and self.worst_e < 450:
                rec = step(pct_now, +1)
        elif (self.base[s] and part < 0.5 * self.base[s][0] and st["mkt"] >= 0.7 * self.base[s][1]
              and cps >= 0):
            mode = "CROWDED"   # our share fell while the market traded: hold, do not tighten
        if rec is not None and blk <= self.cooldown[s]:
            rec = None
        if rec is not None:
            self.cooldown[s] = blk + 2
        lo, hi = blk * BLOCK // 120, (blk + 1) * BLOCK // 120
        self.log.warning(
            "BLOCK min %d-%d %s: filled %d sh at pct %s | clean %d sh, NET %s c/sh ($%.2f) | "
            "share of market %.0f%%%s | excluded news %d stale %d | %s%s",
            lo, hi, s, st["sh"], sorted(st["pct"]) or "-", st["clean"],
            f"{cps:+.2f}" if cps is not None else "n/a", st["net_c"] / 100, part * 100,
            f" (base {self.base[s][0] * 100:.0f}%)" if self.base[s] else "",
            st["news"], st["stale"], mode,
            f" -> RECOMMEND {s.lower()}_pct {pct_now} -> {rec}" if rec is not None else "")
