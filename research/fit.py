"""Fit a compact per-symbol linear model of the 60 s mid change, in raw units, computable live.
Prints OOS R^2 (train odd / test even) and the coefficients fit on all 24 sessions as JSON."""
import json
import sys
import numpy as np
import pandas as pd

data, H = sys.argv[1], 120
SYMS = ["HPPO", "BNKY", "CRRT", "BRRY"]
FEATS = {
    "HPPO": ["dev60", "dev240", "ret20", "flow60"],
    "BNKY": ["dev60", "dev240", "ret20", "flow60", "x20_HPPO", "gap"],
    "CRRT": ["dev60", "dev240", "ret20", "flow60", "x20_HPPO"],
    "BRRY": ["dev60", "dev240", "ret20", "flow60", "x20_HPPO", "gap"],
}


def ema(x, n):  # same recursion the strategy uses: e += (x - e) / n
    return x.ewm(alpha=1.0 / n, adjust=False).mean()


def build(name):
    t = pd.read_csv(f"{data}/{name}.top.csv")
    tr = pd.read_csv(f"{data}/{name}.trades.csv")
    piv = lambda c: t.pivot(index="batch", columns="symbol", values=c)[SYMS]
    mid = ((piv("bid_px") + piv("ask_px")) / 2).ffill()
    tr["signed"] = np.where(tr.aggressor == "BUY", 1, -1) * tr.qty
    flow = tr.pivot_table(index="batch", columns="symbol", values="signed", aggfunc="sum") \
             .reindex(index=mid.index, columns=SYMS).fillna(0)
    gap = mid.HPPO - mid.BNKY - 2 * mid.BRRY
    out = {}
    for s in SYMS:
        f = pd.DataFrame(index=mid.index)
        f["dev60"] = mid[s] - ema(mid[s], 60)
        f["dev240"] = mid[s] - ema(mid[s], 240)
        f["ret20"] = mid[s].diff(20)
        f["flow60"] = flow[s].rolling(60, min_periods=1).sum()
        f["x20_HPPO"] = mid.HPPO.diff(20)
        f["gap"] = gap
        f["y"] = mid[s].shift(-H) - mid[s]
        out[s] = f.iloc[240:-H].dropna()
    return out


sess = {i: build(f"s{i:02d}") for i in range(1, 25)}
coefs = {}
for s in SYMS:
    X = FEATS[s]
    get = lambda ids: pd.concat([sess[i][s] for i in ids])
    a, b = get(range(1, 25, 2)), get(range(2, 25, 2))
    fitb = lambda d: np.linalg.lstsq(np.c_[d[X].values], d.y.values, rcond=None)[0]  # no intercept
    beta = fitb(a)
    pred = b[X].values @ beta
    r2 = 1 - ((b.y - pred) ** 2).sum() / ((b.y - b.y.mean()) ** 2).sum()
    allb = fitb(get(range(1, 25)))
    # per-session OOS stability: corr of pred and y on each test session
    per = [np.corrcoef(sess[i][s][X].values @ beta, sess[i][s].y)[0, 1] for i in range(2, 25, 2)]
    print(f"{s}: OOS R^2 {r2:.3f}, per-test-session corr min {min(per):.2f} median {np.median(per):.2f}")
    coefs[s] = dict(zip(X, [round(float(v), 5) for v in allb]))
    coefs_tr = globals().setdefault("coefs_tr", {})
    coefs_tr[s] = dict(zip(X, [round(float(v), 5) for v in beta]))
print(json.dumps(coefs, indent=1))
json.dump(coefs, open("coef_all.json", "w"))
json.dump(coefs_tr, open("coef_odd.json", "w"))
