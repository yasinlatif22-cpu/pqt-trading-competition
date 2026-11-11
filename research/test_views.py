"""Panel news views: one order per switch-on, right side and size, no self-trades, no quoting against the view."""
import importlib.util, sys
from pqt_client.backtest_v2 import load_session
from pqt_client.v2 import Strategy, backtest_strategy
spec = importlib.util.spec_from_file_location("strat", sys.argv[1]); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
Base = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")
sent, fills, obs = [], [], {}
class T(Base):
    SCHEDULE = {2000: {"view_brry": 1}, 2100: {"view_brry": 0}, 2500: {"view_hppo": -1}, 2600: {"view_hppo": 0}}
    def on_start(self):
        self._mp = {p.name: p.default for p in self.MODEL_PARAMS}; super().on_start()
    @property
    def model_params(self): return dict(self._mp)
    def buy(self, sym, qty, price, ioc=False, **k):
        if ioc: sent.append((self.batch, sym, "BUY", qty, price))
        return super().buy(sym, qty, price, ioc=ioc, **k)
    def sell(self, sym, qty, price, ioc=False, **k):
        if ioc: sent.append((self.batch, sym, "SELL", qty, price))
        return super().sell(sym, qty, price, ioc=ioc, **k)
    def on_fill(self, f):
        fills.append(f); return super().on_fill(f)
    def on_batch(self, st):
        if st.batch in self.SCHEDULE: self._mp.update(self.SCHEDULE[st.batch])
        if st.batch == 1999: obs["ask0"] = st.top["BRRY"].ask_px
        r = super().on_batch(st)
        if 2005 <= st.batch < 2100:
            obs.setdefault("brry_asks_during", 0)
            obs["brry_asks_during"] += sum(1 for o in self.open_orders("BRRY") if o.side == "SELL" and str(o.client_id).startswith("yl"))
        if 2505 <= st.batch < 2600:
            obs.setdefault("hppo_bids_during", 0)
            obs["hppo_bids_during"] += sum(1 for o in self.open_orders("HPPO") if o.side == "BUY" and str(o.client_id).startswith("yl"))
        if st.batch == 2150:
            obs["brry_asks_after"] = sum(1 for o in self.open_orders("BRRY") if o.side == "SELL")
        return r
r = backtest_strategy(load_session(sys.argv[2], "s01"), T())
print("view orders sent:", sent)
selftr = [f for f in fills if f.self_trade]
ok = (len(sent) == 2 and sent[0][1:3] == ("BRRY", "BUY") and sent[1][1:3] == ("HPPO", "SELL")
      and 0 < sent[0][3] <= 50 and sent[0][0] == 2000 and not selftr
      and obs["brry_asks_during"] == 0 and obs["hppo_bids_during"] == 0 and obs["brry_asks_after"] > 0)
print("self-trades:", len(selftr), "| obs:", obs, "| rejects:", r.rejects if not hasattr(r.rejects, '__len__') else len(r.rejects))
print("VIEW TEST", "PASSED" if ok else "FAILED")
