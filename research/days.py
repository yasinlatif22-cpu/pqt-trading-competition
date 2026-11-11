"""Market structure by 8-minute trading day (960 batches) and the final 5 minutes."""
import sys
import numpy as np
import pandas as pd

data = sys.argv[1]
SYMS = ["HPPO", "BNKY", "CRRT", "BRRY"]
rows = []
for i in range(1, 25):
    t = pd.read_csv(f"{data}/s{i:02d}.top.csv")
    tr = pd.read_csv(f"{data}/s{i:02d}.trades.csv")
    t["seg"] = np.minimum(t.batch // 960, 5)
    tr["seg"] = np.minimum(tr.batch // 960, 5)
    for s in SYMS:
        x = t[t.symbol == s].copy()
        x["mid"] = (x.bid_px + x.ask_px) / 2
        x["spr"] = x.ask_px - x.bid_px
        x["dm"] = x.mid.diff(120)
        y = tr[tr.symbol == s]
        for seg, g in x.groupby("seg"):
            mins = g.batch.nunique() / 120
            rows.append(dict(s=s, seg=seg, spr=g.spr.median(), vol60=g.dm.std(),
                             vol_per_min=y[y.seg == seg].qty.sum() / mins,
                             depth=(g.bid_qty + g.ask_qty).median()))
d = pd.DataFrame(rows).groupby(["s", "seg"]).mean().round(2)
pd.set_option("display.width", 200)
print("seg 0-4 = trading days (8 min each), seg 5 = final 5 minutes")
print(d.unstack("seg").to_string())
# day-boundary jumps: |mid change| across boundary vs typical 1-batch change
print("\nmean |mid change| over the 2 batches around each day boundary vs elsewhere (cents)")
for s in SYMS:
    jb, je = [], []
    for i in range(1, 25):
        t = pd.read_csv(f"{data}/s{i:02d}.top.csv")
        m = t[t.symbol == s].set_index("batch")
        m = ((m.bid_px + m.ask_px) / 2).ffill()
        d2 = m.diff(2).abs()
        b = [960 * k + 1 for k in range(1, 5)]
        jb += list(d2.reindex(b).dropna())
        je.append(d2.mean())
    print(s, "boundary", round(np.mean(jb), 2), "elsewhere", round(np.mean(je), 2))
