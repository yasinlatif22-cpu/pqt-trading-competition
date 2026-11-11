import sys, importlib.util
from pqt_client.backtest_v2 import load_manifest, load_session
from pqt_client.v2 import backtest_strategy, Strategy

spec = importlib.util.spec_from_file_location("strat", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
base = next(v for v in vars(mod).values()
            if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy)

LOG = []
class Wrapped(base):
    _b = 0
    def on_batch(self, state):
        self._b = state.batch
        return super().on_batch(state)
    def on_fill(self, fill):
        LOG.append((self._b, fill))
        return super().on_fill(fill)

def mid(top):
    if top is None or top.bid_px is None or top.ask_px is None:
        return None
    return (top.bid_px + top.ask_px) / 2

shown = False
tot_proxy = 0.0
names = load_manifest("data").sessions
for name in names:
    LOG.clear()
    s = load_session("data", name)
    r = backtest_strategy(s, Wrapped())
    if LOG and not shown:
        print("sample fill:", LOG[0][1], "| fields:", [a for a in dir(LOG[0][1]) if not a.startswith("_")])
        shown = True
    gross, skipped = 0.0, 0
    for b, f in LOG:
        try:
            side = str(getattr(f, "side", "")).upper()
            q = abs(f.qty) * (-1 if "SELL" in side else 1) if side else f.qty
            m = mid(s.tops[min(b + 120, len(s.tops) - 1)][f.symbol])
            if m is None:
                skipped += 1; continue
            gross += q * (m - f.price) / 100.0
        except AttributeError as e:
            print("field problem:", e); sys.exit(1)
    fees = r.fees_mills / 1000.0
    pen = r.penalties_mills / 1000.0
    proxy = gross - fees - pen
    tot_proxy += proxy
    print(name, "proxy", round(proxy, 2), "| backtest", round(r.pnl_est_mills / 1000, 2),
          "| fills", len(LOG), "skipped", skipped)
print("mean proxy", round(tot_proxy / len(names), 2))
