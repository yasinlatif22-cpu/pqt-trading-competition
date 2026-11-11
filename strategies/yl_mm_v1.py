"""Four-symbol market maker for the PQT competition.

Quotes one bid and one ask on HPPO, BNKY, CRRT and BRRY around a slow average of each mid,
leans against inventory, keeps net exposure away from the 600 limit, and steps back after news.

    pqt backtest strategies/yl_mm.py --data data --name s01
    pqt run strategies/yl_mm.py --mode test --server https://sandbox.princeton-quant.com --key KEY

Live controls (trader GUI model panel, MODEL_PARAMS):
    size_pct    scales every quote size (100 = defaults below)
    widen       extra cents added to every half-spread
    news_pause  batches to pull quotes on a symbol after news names it (or all, if none named)
    active      1 = quote, 0 = cancel everything and stand aside
    adapt       1 = size each symbol by how its own fills score 60 s later (x0.25 .. x1.5),
                0 = fixed sizes (default: on history, cutting by this score did not help)
    hppo_pct, bnky_pct, crrt_pct, brry_pct
                per-symbol size (0 = stop quoting that symbol). Rotate by hand: every 2 minutes
                the log prints each symbol's recent 60 s markout in cents per share; cut a symbol
                that stays negative, raise one that stays clearly positive.
"""

import math
import re
from collections import deque

from pqt_client.v2 import BatchState, ParamKind, ParamSpec, Side, Strategy

FAIR_BATCHES = 30          # EMA of the mid, ~15 s
E_SOFT = 420               # above this |net exposure|, stop quoting the side that adds to it
REPORT_EVERY = 240         # log every 2 minutes
MO_HORIZON = 120           # score each fill against the mid 60 s later, as the exchange does
MO_DECAY = 0.998           # per batch: the score forgets with a ~3 minute half-life
MO_MIN_SHARES = 300        # shares scored before a symbol's size moves off 1x

#            half-spread (c), size, skew (c at cap), cap (shares)
CFG = {
    "HPPO": dict(half=3, size=30, skew=6, cap=150),
    "BNKY": dict(half=2, size=50, skew=3, cap=500),
    "CRRT": dict(half=2, size=50, skew=3, cap=500),
    "BRRY": dict(half=4, size=60, skew=6, cap=300),
}
WEIGHT = {"HPPO": 1.0, "BNKY": 0.15, "CRRT": -0.29, "BRRY": 0.425}


class YLMarketMaker(Strategy):
    MODEL_PARAMS = (
        ParamSpec("size_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("widen", ParamKind.INT, 0, 0, 20),
        ParamSpec("news_pause", ParamKind.INT, 60, 0, 240),
        ParamSpec("active", ParamKind.INT, 1, 0, 1),
        ParamSpec("adapt", ParamKind.INT, 0, 0, 1),
        ParamSpec("hppo_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("bnky_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("crrt_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("brry_pct", ParamKind.INT, 100, 0, 200),
    )

    def on_start(self) -> None:
        self.fair: dict[str, float] = {}
        self.paused_until: dict[str, int] = {s: 0 for s in CFG}
        self.batch = 0
        self.mid: dict[str, float] = {}
        self.pending: deque = deque()                    # fills waiting 60 s to be scored
        self.mo_num = {s: 0.0 for s in CFG}              # decayed sum of qty * markout (cents)
        self.mo_den = {s: 0.0 for s in CFG}              # decayed sum of qty
        self.mult = {s: 1.0 for s in CFG}
        self.rejects: dict[str, int] = {}
        self.log.warning("START %s mode, session %s, %s batches, params %s", self.mode,
                         getattr(self.session, "session_id", "?"), getattr(self.session, "n_batches", "?"),
                         dict(self.model_params))

    # ---- game-day visibility: anything unusual goes to the log as a WARNING ----
    def on_reject(self, reject) -> None:
        reason = str(getattr(reject, "reason", getattr(reject, "code", reject)))
        n = self.rejects.get(reason, 0) + 1
        self.rejects[reason] = n
        if n <= 3 or n % 100 == 0:
            self.log.warning("REJECT #%d %s: %s", n, reason, reject)

    def on_round_state(self, rs) -> None:
        self.log.warning("SESSION %s (batch %s of %s) %s", getattr(rs, "phase", rs),
                         getattr(rs, "batch", "?"), getattr(rs, "n_batches", "?"), getattr(rs, "text", ""))

    def on_broadcast(self, b) -> None:
        self.log.warning("ORGANISERS: %s", b)

    def on_reconnect(self) -> None:
        self.log.warning("RECONNECTED at batch %d; resting orders were kept", self.batch)

    def on_end(self, state) -> None:
        self.log.warning("END: pos %s, est PnL $%.2f, rejects %s | 60s markout %s",
                         {s: self.position(s) for s in CFG}, state.pnl_est_mills / 1000,
                         self.rejects or "none", self.score_line())

    # ---- adaptive sizing: score our own fills like the exchange does, move size to what pays ----
    def on_fill(self, fill) -> None:
        sign = 1 if fill.side is Side.BUY else -1
        self.pending.append((fill.batch, fill.symbol, sign, fill.price, fill.qty))

    def update_scores(self, state: BatchState) -> None:
        for s in CFG:
            top = state.top[s]
            if top.bid_px is not None and top.ask_px is not None:
                self.mid[s] = (top.bid_px + top.ask_px) / 2
            self.mo_num[s] *= MO_DECAY
            self.mo_den[s] *= MO_DECAY
        while self.pending and self.pending[0][0] + MO_HORIZON <= state.batch:
            _, s, sign, px, qty = self.pending.popleft()
            if s in self.mid:
                self.mo_num[s] += qty * sign * (self.mid[s] - px)
                self.mo_den[s] += qty
        for s in CFG:
            if self.mo_den[s] < MO_MIN_SHARES:
                self.mult[s] = 1.0           # not enough evidence yet
                continue
            ps = self.mo_num[s] / self.mo_den[s]   # recent markout, cents per share
            if ps >= 1.0:
                self.mult[s] = 1.5
            elif ps >= 0.25:
                self.mult[s] = 1.0
            elif ps >= -0.25:
                self.mult[s] = 0.5
            else:
                self.mult[s] = 0.25          # keep a small quote so we notice when it recovers

    def score_line(self) -> str:
        adapt = self.model_params["adapt"]
        return " ".join(
            f"{s}:{(self.mo_num[s] / self.mo_den[s]) if self.mo_den[s] else 0:+.2f}c"
            + (f" x{self.mult[s]}" if adapt else "")
            for s in CFG)

    # ---- news: stand aside on the named symbols (all of them if none is named) ----
    def on_news(self, item) -> None:
        text = f"{item.headline} {item.body}".upper()
        named = set(re.findall(r"\b(HPPO|BNKY|CRRT|BRRY)\b", text))
        # HPPO = BNKY + 2 BRRY: news on a component moves the ETF, and news on the ETF moves both
        if "HPPO" in named:
            named |= {"BNKY", "BRRY"}
        elif named & {"BNKY", "BRRY"}:
            named.add("HPPO")
        named = sorted(named)
        # pause from the batch the item was published (a late connect replays old items: no effect)
        until = item.batch + int(self.model_params["news_pause"])
        if until <= self.batch:
            return
        for s in (named or list(CFG)):
            self.paused_until[s] = max(self.paused_until[s], until)
            self.cancel_all(s)
        self.log.warning("NEWS at batch %d, pausing %s until %d: %s", item.batch, named or "ALL",
                         until, item.headline[:80])

    def on_batch(self, state: BatchState) -> None:
        self.batch = state.batch
        self.update_scores(state)
        if (state.batch + 1) % REPORT_EVERY == 0:
            self.log.info("minute %d: pos %s, E %.0f, est PnL $%.2f | 60s markout %s",
                          (state.batch + 1) // 120, {s: self.position(s) for s in CFG},
                          self.exposure(), state.pnl_est_mills / 1000, self.score_line())
        p = self.model_params
        if not p["active"]:
            for s in CFG:
                if self.open_orders(s):
                    self.cancel_all(symbol=s)
            return
        e = self.exposure()
        for sym, c in CFG.items():
            self.quote(state, sym, c, e, p)

    def exposure(self) -> float:
        return sum(WEIGHT[s] * self.position(s) for s in CFG)

    def quote(self, state: BatchState, sym: str, c: dict, e: float, p) -> None:
        top = state.top[sym]
        if top.halted or top.mid is None or top.bid_px is None or top.ask_px is None:
            return
        f = self.fair.get(sym)
        f = top.mid if f is None else f + (top.mid - f) / FAIR_BATCHES
        self.fair[sym] = f
        if state.batch < self.paused_until[sym]:
            return  # quotes were cancelled when the news arrived; fair keeps updating
        pos = self.position(sym)
        half = c["half"] + int(p["widen"])
        centre = f - c["skew"] * pos / c["cap"]
        bid = min(math.floor(centre - half), top.ask_px - 1)
        ask = max(math.ceil(centre + half), top.bid_px + 1)
        mult = self.mult[sym] if p["adapt"] else 1.0
        sym_pct = p[f"{sym.lower()}_pct"]
        if sym_pct == 0 or p["size_pct"] == 0:   # symbol switched off from the panel
            self.keep(sym, Side.BUY, None, 0)
            self.keep(sym, Side.SELL, None, 0)
            return
        size = max(1, min(int(c["size"] * p["size_pct"] / 100 * sym_pct / 100 * mult),
                          self.symbol_info[sym].max_order_qty))
        w = WEIGHT[sym]
        buy_ok = pos + size <= c["cap"] and not (e > E_SOFT and w > 0) and not (e < -E_SOFT and w < 0)
        sell_ok = pos - size >= -c["cap"] and not (e < -E_SOFT and w > 0) and not (e > E_SOFT and w < 0)
        if p["size_pct"] == 0:
            buy_ok = sell_ok = False
        self.keep(sym, Side.BUY, bid if buy_ok else None, size)
        self.keep(sym, Side.SELL, ask if sell_ok else None, size)

    def keep(self, sym: str, side: Side, price: int | None, size: int) -> None:
        mine = [o for o in self.open_orders(sym) if o.side is side]
        for extra in mine[1:]:
            self.cancel(order_id=extra.order_id)
        if price is None:
            if mine:
                self.cancel(order_id=mine[0].order_id)
        elif not mine:
            (self.buy if side is Side.BUY else self.sell)(sym, size, price)
        elif mine[0].price != price:
            self.replace(order_id=mine[0].order_id, price=price, qty=size)
