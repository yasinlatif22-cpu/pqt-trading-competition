"""Stress sessions: splice price jumps (with or without a news item) and a halt into recorded sessions,
then score the strategy the way the exchange does.

    python shock.py STRATEGY.py DATA_DIR OUT_DIR

Each stressed session gets, on top of the recorded market:
  batch 1200  news "BRRY ..."  -> BRRY +JUMP c, HPPO +2*JUMP c   (prices move 2 batches after the news)
  batch 2400  news "BNKY ..."  -> BNKY -JUMP c, HPPO -JUMP c
  batch 3600  news (no symbol) -> CRRT +JUMP c
  batch 4200-4260  BRRY halted (no quotes, no trades)

Variants: handler on (news_pause 20), handler off (news_pause 0), and the same jumps with NO news item.
Score = 60 s markout per fill minus fees; as the exchange does, a fill made before a news item is not
charged for that item's jump (fills after it are: those are stale quotes being picked off).
"""
import csv
import importlib.util
import json
import os
import shutil
import sys
from multiprocessing import Pool

JUMP = int(os.environ.get("JUMP", 40))
LAG = 2
RAMP = int(os.environ.get("RAMP", 1))  # batches for the price to finish moving after the news
H = 120
SESSIONS = [f"s{i:02d}" for i in range(1, 25, 3)]  # 8 sessions
CROSS = float(os.environ.get("CROSS", 0))  # CRRT moves CROSS x the HPPO-equivalent jump (official weight 0.29)
SHOCKS = [  # (news batch, headline, {symbol: jump cents})
    (1200, "BRRY wins major contract", {"BRRY": JUMP, "HPPO": 2 * JUMP, "CRRT": -round(CROSS * 2 * JUMP)}),
    (2400, "BNKY cuts guidance", {"BNKY": -JUMP, "HPPO": -JUMP, "CRRT": round(CROSS * JUMP)}),
    (3600, "Regulator announces sector review", {"CRRT": JUMP}),
]
HALT = ("BRRY", 4200, 4260)


def build(src, out):
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    m = json.load(open(f"{src}/manifest.json"))
    m["sessions"] = [s for s in m["sessions"] if (s if isinstance(s, str) else s.get("name", s.get("session"))) in SESSIONS] \
        if m["sessions"] and not isinstance(m["sessions"][0], str) else SESSIONS
    json.dump(m, open(f"{out}/manifest.json", "w"), indent=1)

    def shift(sym, b):  # the price walks to its new level over RAMP batches
        return sum(round(j.get(sym, 0) * min(1.0, max(0, b - nb - LAG + 1) / RAMP)) for nb, _, j in SHOCKS)

    for s in SESSIONS:
        with open(f"{src}/{s}.top.csv") as fi, open(f"{out}/{s}.top.csv", "w", newline="") as fo:
            r = csv.DictReader(fi); w = csv.DictWriter(fo, r.fieldnames); w.writeheader()
            for row in r:
                b, sym = int(row["batch"]), row["symbol"]
                d = shift(sym, b)
                for k in ("bid_px", "ask_px"):
                    if row[k]:
                        row[k] = str(int(row[k]) + d)
                if sym == HALT[0] and HALT[1] <= b < HALT[2]:
                    row["status"] = "HALTED"
                w.writerow(row)
        with open(f"{src}/{s}.trades.csv") as fi, open(f"{out}/{s}.trades.csv", "w", newline="") as fo:
            r = csv.DictReader(fi); w = csv.DictWriter(fo, r.fieldnames); w.writeheader()
            for row in r:
                b, sym = int(row["batch"]), row["symbol"]
                if sym == HALT[0] and HALT[1] <= b < HALT[2]:
                    continue
                row["price"] = str(int(row["price"]) + shift(sym, b))
                w.writerow(row)


def run(args):
    strat, data, name, variant = args
    from pqt_client.backtest_v2 import load_session
    from pqt_client.v2 import NewsItem, Strategy, backtest_strategy
    spec = importlib.util.spec_from_file_location("strat", strat)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    base = next(v for v in vars(mod).values()
                if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")
    log = []
    news_on = variant != "no news item"

    class W(base):
        _b = 0

        def on_batch(self, st):
            self._b = st.batch
            if news_on:
                for nb, head, _ in SHOCKS:
                    if st.batch == nb:
                        self.on_news(NewsItem(news_id=nb, batch=nb, headline=head, body=""))
            return super().on_batch(st)

        def on_fill(self, f):
            log.append((self._b, f))
            return super().on_fill(f)

    s = load_session(data, name)
    r = backtest_strategy(s, W())
    total, window = 0.0, 0.0
    for b, f in log:
        q = f.qty if f.side.name == "BUY" else -f.qty
        top = s.tops[min(b + H, len(s.tops) - 1)][f.symbol]
        if top.bid_px is None or top.ask_px is None:
            continue
        g = q * ((top.bid_px + top.ask_px) / 2 - f.price)
        for nb, _, j in SHOCKS:
            if news_on and b < nb <= b + H and f.symbol in j:
                g -= q * j[f.symbol]  # exchange removes a later news jump from earlier fills
            if nb <= b < nb + max(20, RAMP + 20) and f.symbol in j:
                window += g / 100
        total += g / 100
    return variant, name, total - (r.fees_mills + r.penalties_mills) / 1000, window, len(r.rejects) if hasattr(r.rejects, "__len__") else r.rejects


if __name__ == "__main__":
    strat, src, out = sys.argv[1:4]
    build(src, out)
    off = os.path.join(out, "_strategy_news_off.py")
    open(off, "w").write(open(strat).read().replace('ParamSpec("news_pause", ParamKind.INT, 20,', 'ParamSpec("news_pause", ParamKind.INT, 0,'))
    variants = ["handler on", "handler off", "no news item"]
    with Pool(8) as p:
        res = p.map(run, [(off if v == "handler off" else strat, out, n, v) for v in variants for n in SESSIONS])
    base = {}
    print(f"jump {JUMP}c over {RAMP} batches, {len(SESSIONS)} stressed sessions")
    print(f"{'variant':14} {'score/session':>14} {'P&L in 10 s after each shock':>30} {'rejects':>8}")
    for v in variants:
        rr = [x for x in res if x[0] == v]
        print(f"{v:14} {sum(x[2] for x in rr)/len(rr):14.0f} {sum(x[3] for x in rr)/len(rr):30.0f} {sum(x[4] for x in rr):8}")
