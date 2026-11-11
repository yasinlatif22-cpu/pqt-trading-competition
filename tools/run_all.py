import sys, importlib.util
from pqt_client.backtest_v2 import load_manifest, load_session
from pqt_client.v2 import backtest_strategy, Strategy

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("strat", path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
cls = next(v for v in vars(mod).values()
           if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy)

total = 0
for name in load_manifest("data").sessions:
    r = backtest_strategy(load_session("data", name), cls())
    pnl = r.pnl_est_mills / 1000
    total += pnl
    print(name, round(pnl, 2), r.fills, dict(r.positions))
print("mean", round(total / 24, 2))
