"""Four-symbol market maker that quotes around a regression forecast of each mid 60 s ahead.

Fit on the 24 historical sessions (research/fit.py), the forecasts say:
  * prices trend over a minute: a mid above its 30 s average tends to keep rising (momentum),
    so quoting around a lagging average gets run over; we shift the centre toward the trend;
  * HPPO leads: BRRY catches up to the basket gap (HPPO - BNKY - 2 BRRY) and to HPPO's last
    10 s move; BNKY follows HPPO; CRRT moves against HPPO;
  * aggressive order flow over 30 s continues.

    pqt backtest strategies/yl_alpha.py --data data --name s01
    pqt run strategies/yl_alpha.py --mode test --server https://sandbox.princeton-quant.com --key KEY

Live controls (trader GUI model panel):
    alpha_pct   how much of the forecast to trust (100 = full, 0 = plain market maker)
    take_c      cross the spread when the forecast beats the far touch by this many cents (0 = off)
    size_pct    scales every quote size
    widen       extra cents on every half-spread
    news_pause  batches to stand aside on a symbol after news names it (all symbols if none named)
    active      0 cancels everything
"""

import json
import math
import os
from collections import deque

from pqt_client.v2 import BatchState, ParamKind, ParamSpec, Side, Strategy

SYMS = ("HPPO", "BNKY", "CRRT", "BRRY")
WEIGHT = {"HPPO": 1.0, "BNKY": 0.15, "CRRT": -0.29, "BRRY": 0.425}
E_SOFT = 420
REPORT_EVERY = 240

# Coefficients of the 60 s mid-change forecast (cents), fit on all 24 historical sessions.
COEF = {
    "HPPO": {"dev60": 0.28262, "dev240": -0.00085, "ret20": 0.51503, "flow60": 0.00198},
    "BNKY": {"dev60": 0.30204, "dev240": -0.06259, "ret20": 0.41334, "flow60": 0.00028,
             "x20_HPPO": 0.15528, "gap": 0.00306},
    "CRRT": {"dev60": 0.3615, "dev240": -0.09588, "ret20": 0.09306, "flow60": 0.00036,
             "x20_HPPO": -0.29647},
    "BRRY": {"dev60": -0.01572, "dev240": 0.07006, "ret20": 0.05898, "flow60": 0.00016,
             "x20_HPPO": 0.41314, "gap": 0.28641},
}
if os.environ.get("PQT_COEF"):
    COEF = json.load(open(os.environ["PQT_COEF"]))

#          half-spread (c), size, skew (c at cap), cap
CFG = {
    "HPPO": dict(half=3, size=30, skew=6, cap=300, quote=0),  # taker-only on the forecast
    "BNKY": dict(half=2, size=50, skew=3, cap=500),
    "CRRT": dict(half=2, size=50, skew=3, cap=500),
    "BRRY": dict(half=4, size=60, skew=6, cap=300),
}
if os.environ.get("PQT_CFG"):
    for _s, _d in json.loads(os.environ["PQT_CFG"]).items():
        CFG[_s].update(_d)
DEFAULT_ALPHA = int(os.environ.get("PQT_ALPHA", 100))
DEFAULT_TAKE = int(os.environ.get("PQT_TAKE", 2))
DEFAULT_FILTER = int(os.environ.get("PQT_FILTER", 999))  # 999 = centre quotes on the 15 s EMA, no filter
FAIR_BATCHES = 30  # slow EMA centre used in filter mode (same as yl_mm)
TAKE_SYMS = set(os.environ.get("PQT_TAKE_SYMS", "HPPO").split(","))


class YLAlpha(Strategy):
    MODEL_PARAMS = (
        ParamSpec("alpha_pct", ParamKind.INT, DEFAULT_ALPHA, 0, 200),
        ParamSpec("take_c", ParamKind.INT, DEFAULT_TAKE, 0, 50),
        ParamSpec("filter_c", ParamKind.INT, DEFAULT_FILTER, 0, 999),
        ParamSpec("size_pct", ParamKind.INT, 100, 0, 200),
        ParamSpec("widen", ParamKind.INT, 0, 0, 20),
        ParamSpec("news_pause", ParamKind.INT, 20, 0, 240),
        ParamSpec("active", ParamKind.INT, 1, 0, 1),
    )

    def on_start(self) -> None:
        self.mid = {}
        self.e60 = {}
        self.e240 = {}
        self.hist = {s: deque(maxlen=21) for s in SYMS}
        self.flows = {s: deque(maxlen=60) for s in SYMS}
        self.paused_until = {s: 0 for s in SYMS}
        self.batch = 0
        self.n = 0

    def on_news(self, item) -> None:
        text = f"{getattr(item, 'headline', '')} {getattr(item, 'body', '')}".upper()
        named = [s for s in SYMS if s in text]
        until = self.batch + int(self.model_params["news_pause"])
        for s in (named or SYMS):
            self.paused_until[s] = max(self.paused_until[s], until)
            self.cancel_all(symbol=s)
        self.log.info("news at batch %d, pausing %s: %s", self.batch, named or "ALL",
                      str(getattr(item, "headline", ""))[:80])

    # ---------------------------------------------------------------- features
    def update_features(self, state: BatchState) -> None:
        flow = {s: 0 for s in SYMS}
        for t in state.trades:
            if t.symbol in flow and not t.self_trade:
                buy = "BUY" in str(getattr(t.aggressor, "name", t.aggressor)).upper()
                flow[t.symbol] += t.qty if buy else -t.qty
        for s in SYMS:
            self.flows[s].append(flow[s])
            top = state.top[s]
            if top.bid_px is not None and top.ask_px is not None and not top.halted:
                self.mid[s] = (top.bid_px + top.ask_px) / 2
            m = self.mid.get(s)
            if m is None:
                continue
            if s not in self.e60:
                self.e60[s] = self.e240[s] = m
            self.e30 = getattr(self, "e30", {})
            self.e30[s] = m if s not in self.e30 else self.e30[s] + (m - self.e30[s]) / FAIR_BATCHES
            self.e60[s] += (m - self.e60[s]) / 60
            self.e240[s] += (m - self.e240[s]) / 240
            self.hist[s].append(m)
        self.n += 1

    def forecast(self, s: str) -> float:
        if self.n < 60 or s not in self.mid or len(self.hist["HPPO"]) < 21:
            return 0.0
        m = self.mid[s]
        f = {
            "dev60": m - self.e60[s],
            "dev240": m - self.e240[s],
            "ret20": m - self.hist[s][0] if len(self.hist[s]) == 21 else 0.0,
            "flow60": sum(self.flows[s]),
            "x20_HPPO": self.hist["HPPO"][-1] - self.hist["HPPO"][0],
        }
        if all(k in self.mid for k in ("HPPO", "BNKY", "BRRY")):
            f["gap"] = self.mid["HPPO"] - self.mid["BNKY"] - 2 * self.mid["BRRY"]
        return sum(c * f.get(k, 0.0) for k, c in COEF[s].items())

    # ---------------------------------------------------------------- trading
    def on_batch(self, state: BatchState) -> None:
        self.batch = state.batch
        self.update_features(state)
        if (state.batch + 1) % REPORT_EVERY == 0:
            self.log.info("minute %d: pos %s, E %.0f, est PnL $%.2f",
                          (state.batch + 1) // 120, {s: self.position(s) for s in SYMS},
                          self.exposure(), state.pnl_est_mills / 1000)
        p = self.model_params
        if not p["active"]:
            for s in SYMS:
                if self.open_orders(s):
                    self.cancel_all(symbol=s)
            return
        e = self.exposure()
        for s in SYMS:
            self.trade(state, s, CFG[s], e, p)

    def exposure(self) -> float:
        return sum(WEIGHT[s] * self.position(s) for s in SYMS)

    def trade(self, state: BatchState, s: str, c: dict, e: float, p) -> None:
        top = state.top[s]
        if top.halted or top.bid_px is None or top.ask_px is None or s not in self.mid:
            return
        if state.batch < self.paused_until[s]:
            return
        fc = self.forecast(s)
        filt = int(p["filter_c"])
        if filt > 0:  # filter mode: centre on the slow EMA, use the forecast only to pull a side
            fair = self.e30[s]
        else:
            fair = self.mid[s] + p["alpha_pct"] / 100.0 * fc
        pos = self.position(s)
        half = c["half"] + int(p["widen"])
        centre = fair - c["skew"] * pos / c["cap"]
        size = max(1, min(int(c["size"] * p["size_pct"] / 100), self.symbol_info[s].max_order_qty))
        w = WEIGHT[s]
        buy_ok = pos + size <= c["cap"] and not (e > E_SOFT and w > 0) and not (e < -E_SOFT and w < 0)
        sell_ok = pos - size >= -c["cap"] and not (e < -E_SOFT and w > 0) and not (e > E_SOFT and w < 0)
        if p["size_pct"] == 0:
            buy_ok = sell_ok = False
        if filt > 0:
            if fc >= filt:     # forecast up: a resting ask would be sold into a rise
                sell_ok = False
            elif fc <= -filt:  # forecast down: a resting bid would be bought into a fall
                buy_ok = False

        take = int(p["take_c"])
        if take > 0 and s in TAKE_SYMS:
            tsize = min(c.get("take_size", size), self.symbol_info[s].max_order_qty)
            buy_ok_t = pos + tsize <= c["cap"] and not (e > E_SOFT and w > 0) and not (e < -E_SOFT and w < 0)
            sell_ok_t = pos - tsize >= -c["cap"] and not (e < -E_SOFT and w > 0) and not (e > E_SOFT and w < 0)
            edge_up = self.mid[s] + fc - top.ask_px
            edge_dn = top.bid_px - (self.mid[s] + fc)
            if buy_ok_t and edge_up >= take:
                self.buy(s, tsize, top.ask_px, ioc=True)
                buy_ok = False
            elif sell_ok_t and edge_dn >= take:
                self.sell(s, tsize, top.bid_px, ioc=True)
                sell_ok = False
        if False:
            if buy_ok and fair - top.ask_px >= take:
                self.buy(s, size, top.ask_px, ioc=True)
                buy_ok = False
            elif sell_ok and top.bid_px - fair >= take:
                self.sell(s, size, top.bid_px, ioc=True)
                sell_ok = False

        if not c.get("quote", 1):  # taker-only symbol: never rest passive orders
            buy_ok = sell_ok = False
        bid = min(math.floor(centre - half), top.ask_px - 1)
        ask = max(math.ceil(centre + half), top.bid_px + 1)
        self.keep(s, Side.BUY, bid if buy_ok else None, size)
        self.keep(s, Side.SELL, ask if sell_ok else None, size)

    def keep(self, s: str, side: Side, price, size: int) -> None:
        mine = [o for o in self.open_orders(s) if o.side is side]
        for extra in mine[1:]:
            self.cancel(order_id=extra.order_id)
        if price is None:
            if mine:
                self.cancel(order_id=mine[0].order_id)
        elif not mine:
            (self.buy if side is Side.BUY else self.sell)(s, size, price)
        elif mine[0].price != price:
            self.replace(order_id=mine[0].order_id, price=price, qty=size)
