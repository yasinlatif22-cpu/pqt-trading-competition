"""Focused tests for yl_mm v2 controls, run through the SDK backtester on recorded session s01.

    python test_controls.py STRATEGY.py DATA_DIR

Covers: size cut resizes resting orders, partial fills are not topped up early, a symbol switched
off while its book is one-sided, active=0, a teammate's manual order is left alone, news missed by
on_news (reconnect) is still handled, reconnect re-anchors fair, news pause + re-anchor.
"""
import csv
import dataclasses
import importlib.util
import json
import os
import shutil
import sys
import tempfile

from pqt_client.backtest_v2 import load_session
from pqt_client.v2 import NewsItem, Side, Strategy, backtest_strategy

strat_path, data = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("strat", strat_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
Base = next(v for v in vars(mod).values()
            if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")
CFG = mod.CFG
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def ours(strategy, sym=None):
    return [o for o in strategy.open_orders(sym) if str(o.client_id or "").startswith("yl")]


class Harness(Base):
    """Lets a test change panel parameters at given batches and run hooks."""
    SCHEDULE: dict = {}          # batch -> {param: value}
    HOOKS: dict = {}             # batch -> fn(self, state) -> state | None (before strategy)
    AFTER: dict = {}             # batch -> fn(self, state) (after strategy)

    def on_start(self):
        self._mp = {p.name: p.default for p in self.MODEL_PARAMS}
        super().on_start()

    @property
    def model_params(self):
        return dict(self._mp)

    def on_batch(self, state):
        if state.batch in self.SCHEDULE:
            self._mp.update(self.SCHEDULE[state.batch])
        if state.batch in self.HOOKS:
            state = self.HOOKS[state.batch](self, state) or state
        r = super().on_batch(state)
        if state.batch in self.AFTER:
            self.AFTER[state.batch](self, state)
        return r


def run(cls, data_dir=data, name="s01"):
    return backtest_strategy(load_session(data_dir, name), cls())


# 1. size cut: every resting order must shrink to the new size within 2 batches (same price is fine)
obs = {}
class T1(Harness):
    SCHEDULE = {1000: {"size_pct": 200}, 1500: {"size_pct": 50}}
    AFTER = {1499: lambda self, st: obs.update(before={o.symbol: o.remaining for o in ours(self)}),
             1502: lambda self, st: obs.update(after=[(o.symbol, o.remaining) for o in ours(self)])}
run(T1)
limit = {s: max(1, int(c["size"] * 0.5)) for s, c in CFG.items()}
bad = [(s, q) for s, q in obs["after"] if q > limit[s]]
check("size cut 200% -> 50% resizes resting orders", not bad and bool(obs["after"]),
      f"before {obs['before']} after {obs['after']} limits {limit}")

# 2. partial fills: no same-price top-up unless remaining < size/3
replaces = []
class T2(Harness):
    def replace(self, *, price, qty, order_id=None, client_id=None):
        o = next((x for x in self.open_orders() if x.order_id == order_id), None)
        if o is not None and o.price == price and qty > o.remaining:
            replaces.append((o.symbol, o.remaining, qty))
        return super().replace(price=price, qty=qty, order_id=order_id, client_id=client_id)
run(T2)
early = [r for r in replaces if r[1] * 3 >= r[2]]
check("partly filled orders keep queue priority (no early top-up)", not early,
      f"{len(replaces)} same-price top-ups, {len(early)} with remaining >= size/3")

# 3. symbol off while its book is one-sided (BRRY bid missing for batches 2000-2100)
tmp = tempfile.mkdtemp()
m = json.load(open(f"{data}/manifest.json"))
json.dump(m, open(f"{tmp}/manifest.json", "w"))
shutil.copy(f"{data}/s01.trades.csv", f"{tmp}/s01.trades.csv")
with open(f"{data}/s01.top.csv") as fi, open(f"{tmp}/s01.top.csv", "w", newline="") as fo:
    r = csv.DictReader(fi); w = csv.DictWriter(fo, r.fieldnames); w.writeheader()
    for row in r:
        if row["symbol"] == "BRRY" and 2000 <= int(row["batch"]) < 2100:
            row["bid_px"] = ""; row["bid_qty"] = ""
        w.writerow(row)
obs3 = {}
class T3(Harness):
    SCHEDULE = {2001: {"brry_pct": 0}, 2200: {"brry_pct": 100}}
    AFTER = {2000: lambda self, st: obs3.update(before=len(ours(self, "BRRY"))),
             2003: lambda self, st: obs3.update(off=len(ours(self, "BRRY"))),
             2150: lambda self, st: obs3.update(still_off=len(ours(self, "BRRY"))),
             2230: lambda self, st: obs3.update(back=len(ours(self, "BRRY")))}
run(T3, tmp)
check("brry_pct=0 cancels BRRY even with a one-sided book", obs3.get("before", 0) > 0 and obs3["off"] == 0
      and obs3["still_off"] == 0 and obs3["back"] > 0, str(obs3))

# 4. active=0 cancels all of ours within a batch; 5. a manual (non-yl) order is never touched
obs4 = {}
def place_manual(self, st):
    top = st.top["BNKY"]
    Strategy.buy(self, "BNKY", 5, top.bid_px - 200, client_id="gui-manual-1")
class T4(Harness):
    HOOKS = {500: place_manual}
    SCHEDULE = {3000: {"active": 0}, 3100: {"active": 1}}
    AFTER = {700: lambda self, st: obs4.update(manual_700=[o.client_id for o in self.open_orders("BNKY") if not str(o.client_id).startswith("yl")]),
             3002: lambda self, st: obs4.update(ours_off=len(ours(self)),
                                                manual_off=[o.client_id for o in self.open_orders() if not str(o.client_id).startswith("yl")]),
             3130: lambda self, st: obs4.update(ours_back=len(ours(self)))}
run(T4)
check("active=0 cancels all of our orders, then resumes", obs4["ours_off"] == 0 and obs4["ours_back"] > 0, str(obs4))
check("teammate's manual order is never cancelled", obs4["manual_700"] == ["gui-manual-1"]
      and obs4["manual_off"] == ["gui-manual-1"], str(obs4))

# 6. news missed by on_news (arrives only in news_stack, as after a reconnect) still pauses quotes
item = NewsItem(news_id=77, batch=1800, headline="CRRT recalls product", body="")
obs6 = {}
def inject(self, st):
    return dataclasses.replace(st, news_stack=tuple(st.news_stack) + (item,))
class T6(Harness):
    HOOKS = {b: inject for b in range(1800, 1900)}
    AFTER = {1799: lambda self, st: obs6.update(before=len(ours(self, "CRRT"))),
             1803: lambda self, st: obs6.update(during=len(ours(self, "CRRT")), bnky=len(ours(self, "BNKY"))),
             1900: lambda self, st: obs6.update(back=len(ours(self, "CRRT")))}
run(T6)
check("news seen only in news_stack (missed on reconnect) still pauses the symbol",
      obs6["before"] > 0 and obs6["during"] == 0 and obs6["back"] > 0, str(obs6))

# 7. reconnect: quotes hold off SETTLE batches, then fair is re-anchored to the live mid
obs7 = {}
def recon(self, st):
    self.fair["BRRY"] = self.fair["BRRY"] - 300   # pretend fair went badly stale while away
    self.on_reconnect()
def snap7(self, st):
    top = st.top["BRRY"]
    obs7["fair_minus_mid"] = round(self.fair["BRRY"] - (top.bid_px + top.ask_px) / 2, 2)
class T7(Harness):
    HOOKS = {2500: recon}
    AFTER = {2500 + getattr(mod, "SETTLE", 4) - 1: snap7}
run(T7)
check("reconnect re-anchors fair to the live mid", abs(obs7["fair_minus_mid"]) < 1.0, str(obs7))

# 8. explicit news via on_news pauses the right symbols and re-anchors on return
obs8 = {}
def news8(self, st):
    self.on_news(NewsItem(news_id=88, batch=st.batch, headline="BRRY guidance cut", body=""))
    self.fair["BRRY"] += 500   # stale fair: must not survive the pause
pause = next(p.default for p in Base.MODEL_PARAMS if p.name == "news_pause")
class T8(Harness):
    HOOKS = {3300: news8}
    AFTER = {3302: lambda self, st: obs8.update(brry=len(ours(self, "BRRY")), hppo=len(ours(self, "HPPO")),
                                                crrt=len(ours(self, "CRRT"))),
             3300 + pause + getattr(mod, "SETTLE", 4) + 2: lambda self, st: obs8.update(
                 back=len(ours(self, "BRRY")),
                 fair_gap=round(abs(self.fair["BRRY"] - (st.top["BRRY"].bid_px + st.top["BRRY"].ask_px) / 2), 1))}
run(T8)
_all = any(p.name == "news_all" and p.default == 1 for p in Base.MODEL_PARAMS)
check("BRRY news pauses BRRY+HPPO (all symbols if news_all), then re-enters with a re-anchored fair",
      obs8["brry"] == 0 and obs8["hppo"] == 0 and ((obs8["crrt"] == 0) if _all else (obs8["crrt"] > 0)) and obs8["back"] > 0 and obs8["fair_gap"] < 20,
      str(obs8))

print(f"\n{sum(ok for _, ok in RESULTS)}/{len(RESULTS)} passed")
sys.exit(0 if all(ok for _, ok in RESULTS) else 1)
