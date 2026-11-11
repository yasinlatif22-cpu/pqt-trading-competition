"""Does crossing the spread on the forecast pay? Coefficients fit on odd sessions, evaluated on even.
Per trade gain (cents/share) = signed (mid in 60 s - entry price) - 0.4c taker fee."""
import json
import numpy as np
import pandas as pd
import importlib.util
import sys

data = sys.argv[1]
SYMS = ["HPPO", "BNKY", "CRRT", "BRRY"]
coef = json.load(open("coef_odd.json"))
H = 120


def ema(x, n):
    return x.ewm(alpha=1.0 / n, adjust=False).mean()


rows = []
for i in range(2, 25, 2):
    name = f"s{i:02d}"
    t = pd.read_csv(f"{data}/{name}.top.csv")
    tr = pd.read_csv(f"{data}/{name}.trades.csv")
    piv = lambda c: t.pivot(index="batch", columns="symbol", values=c)[SYMS]
    bid, ask = piv("bid_px"), piv("ask_px")
    mid = ((bid + ask) / 2).ffill()
    tr["signed"] = np.where(tr.aggressor == "BUY", 1, -1) * tr.qty
    flow = tr.pivot_table(index="batch", columns="symbol", values="signed", aggfunc="sum") \
             .reindex(index=mid.index, columns=SYMS).fillna(0)
    gap = mid.HPPO - mid.BNKY - 2 * mid.BRRY
    for s in SYMS:
        f = pd.DataFrame({
            "dev60": mid[s] - ema(mid[s], 60), "dev240": mid[s] - ema(mid[s], 240),
            "ret20": mid[s].diff(20), "flow60": flow[s].rolling(60, min_periods=1).sum(),
            "x20_HPPO": mid.HPPO.diff(20), "gap": gap})
        fc = sum(c * f[k] for k, c in coef[s].items())
        # order sent at batch b executes at batch b+1's touch
        buy_px, sell_px = ask[s].shift(-1), bid[s].shift(-1)
        fut = mid[s].shift(-1 - H)
        d = pd.DataFrame({"fc": fc, "half": (ask[s] - bid[s]) / 2,
                          "gbuy": fut - buy_px - 0.4, "gsell": sell_px - fut - 0.4}).iloc[240:-H - 1].dropna()
        d["s"] = s
        rows.append(d)

d = pd.concat(rows)
print("edge = forecast minus half-spread (cents). Gain per share after fee, even (test) sessions:")
print(f"{'sym':5}{'edge>':>6}{'trades/sess':>12}{'avg gain c':>11}{'hit %':>7}{'$/sess @100sh':>15}")
for s in SYMS:
    x = d[d.s == s]
    for thr in (0, 1, 2, 3, 5):
        b = x[x.fc - x.half > thr].gbuy
        sl = x[-x.fc - x.half > thr].gsell
        g = pd.concat([b, sl])
        if len(g) == 0:
            continue
        n = len(g) / 12
        print(f"{s:5}{thr:6}{n:12.0f}{g.mean():11.2f}{(g > 0).mean() * 100:7.0f}{g.mean() * n:15.0f}")
