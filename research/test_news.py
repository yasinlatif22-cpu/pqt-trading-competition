"""Inject fake news into a backtest (the data has none) and check the strategy steps aside and comes back.

    python test_news.py STRATEGY.py DATA_DIR
"""
import importlib.util
import re
import sys

from pqt_client.backtest_v2 import load_session
from pqt_client.v2 import NewsItem, Strategy, backtest_strategy

spec = importlib.util.spec_from_file_location("strat", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
base = next(v for v in vars(mod).values()
            if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")

NEWS = {
    600: NewsItem(news_id=1, batch=600, headline="BRRY recalls flagship product", body="Shares of BRRY ..."),
    1500: NewsItem(news_id=2, batch=1500, headline="Central bank surprises markets", body="Rates up 50bp."),
    2400: NewsItem(news_id=3, batch=2400, headline="BNKY and CRRT announce merger talks", body=""),
}
SYMS = ("HPPO", "BNKY", "CRRT", "BRRY")
import re as _re
PAUSE = int(_re.search(r'ParamSpec\("news_pause", ParamKind.INT, (\d+)', open(sys.argv[1]).read()).group(1))
snap = {}
fills_during = []


class T(base):
    def on_batch(self, state):
        if state.batch in NEWS:
            self.on_news(NEWS[state.batch])
        r = super().on_batch(state)
        for b0 in NEWS:
            for k in (b0, b0 + 1, b0 + 10, b0 + PAUSE - 1, b0 + PAUSE + 1, b0 + PAUSE + 10):
                if state.batch == k:
                    snap[k] = {s: len(self.open_orders(s)) for s in SYMS}
        return r

    def on_fill(self, f):
        fills_during.append((getattr(self, "batch", -1), f.symbol))
        return super().on_fill(f)


r = backtest_strategy(load_session(sys.argv[2], "s01"), T())
ok = True
for b0, item in NEWS.items():
    print(f"\nnews at {b0}: {item.headline!r}")
    for k in sorted(x for x in snap if b0 <= x <= b0 + PAUSE + 10):
        print(f"  batch {k:5}: open orders {snap[k]}")
ALL = bool(re.search(r'ParamSpec\("news_all", ParamKind.INT, 1', open(sys.argv[1]).read()))
expect = {600: set(SYMS) if ALL else {"BRRY", "HPPO"}, 1500: set(SYMS), 2400: set(SYMS) if ALL else {"BNKY", "CRRT", "HPPO"}}
for b0, paused in expect.items():
    during = snap[b0 + 10]
    during_end = snap[b0 + PAUSE - 1]
    after = snap[b0 + PAUSE + 10]
    bad = [s for s in paused if during[s] > 0 or during_end[s] > 0] + [s for s in SYMS if s not in paused and during[s] == 0]
    back = [s for s in SYMS if after[s] == 0]
    if bad or back:
        ok = False
        print(f"FAIL at {b0}: wrong during pause {bad}, not back after {back}")
    stray = [(b, s) for b, s in fills_during if b0 < b < b0 + PAUSE and s in paused]
    if stray:
        ok = False
        print(f"FAIL at {b0}: fills on paused symbols {stray[:5]}")
print("\nrejects:", r.rejects)
print("NEWS TEST PASSED" if ok else "NEWS TEST FAILED")
