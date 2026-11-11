"""Which observable features predict each symbol's mid change over the next H batches?
Train on odd sessions, test on even. Reports out-of-sample R^2 and correlation per feature."""
import sys
import numpy as np
import pandas as pd

data = sys.argv[1]
H = int(sys.argv[2]) if len(sys.argv) > 2 else 120
SYMS = ["HPPO", "BNKY", "CRRT", "BRRY"]


def build(name):
    t = pd.read_csv(f"{data}/{name}.top.csv")
    tr = pd.read_csv(f"{data}/{name}.trades.csv")
    piv = lambda c: t.pivot(index="batch", columns="symbol", values=c)[SYMS]
    bid, ask, bq, aq = piv("bid_px"), piv("ask_px"), piv("bid_qty"), piv("ask_qty")
    mid = ((bid + ask) / 2).ffill()
    micro = ((bid * aq + ask * bq) / (bq + aq)).ffill()
    imb = ((bq - aq) / (bq + aq)).fillna(0)
    tr["signed"] = np.where(tr.aggressor == "BUY", 1, -1) * tr.qty
    flow = tr.pivot_table(index="batch", columns="symbol", values="signed", aggfunc="sum") \
             .reindex(index=mid.index, columns=SYMS).fillna(0)
    feats = {}
    for s in SYMS:
        f = pd.DataFrame(index=mid.index)
        f["micro_dev"] = micro[s] - mid[s]
        f["imb"] = imb[s]
        f["flow10"] = flow[s].rolling(10, min_periods=1).sum()
        f["flow60"] = flow[s].rolling(60, min_periods=1).sum()
        f["ret20"] = mid[s].diff(20)
        f["ret120"] = mid[s].diff(120)
        f["dev_ema60"] = mid[s] - mid[s].ewm(span=60).mean()
        f["dev_ema240"] = mid[s] - mid[s].ewm(span=240).mean()
        gap = mid.HPPO - mid.BNKY - 2 * mid.BRRY  # >0: HPPO rich vs basket
        f["basket_gap"] = gap if s == "HPPO" else (-gap if s in ("BNKY", "BRRY") else 0 * gap)
        f["basket_gap_dm"] = f["basket_gap"] - f["basket_gap"].rolling(600, min_periods=60).mean()
        for o in SYMS:
            if o != s:
                f[f"x_ret20_{o}"] = mid[o].diff(20)
        f["y"] = mid[s].shift(-H) - mid[s]
        feats[s] = f
    return feats


train = {s: [] for s in SYMS}
test = {s: [] for s in SYMS}
for i in range(1, 25):
    d = build(f"s{i:02d}")
    for s in SYMS:
        df = d[s].iloc[240:-H].dropna()
        (train if i % 2 else test)[s].append(df)

pd.set_option("display.width", 220)
for s in SYMS:
    a = pd.concat(train[s]); b = pd.concat(test[s])
    X = [c for c in a.columns if c != "y"]
    # standardize on train
    mu, sd = a[X].mean(), a[X].std().replace(0, 1)
    A = ((a[X] - mu) / sd).values; B = ((b[X] - mu) / sd).values
    A1 = np.c_[np.ones(len(A)), A]; B1 = np.c_[np.ones(len(B)), B]
    beta, *_ = np.linalg.lstsq(A1, a.y.values, rcond=None)
    pred = B1 @ beta
    r2 = 1 - ((b.y - pred) ** 2).sum() / ((b.y - b.y.mean()) ** 2).sum()
    corr_te = {c: b[c].corr(b.y) for c in X}
    corr_tr = {c: a[c].corr(a.y) for c in X}
    out = pd.DataFrame({"coef_std": beta[1:], "corr_train": corr_tr, "corr_test": corr_te}, index=X)
    print(f"\n=== {s}  H={H}  OOS R^2 = {r2:.4f}  (pred/actual corr {np.corrcoef(pred, b.y)[0,1]:.3f})  y sd {b.y.std():.1f}c")
    print(out.round(3).sort_values("corr_test", key=abs, ascending=False).to_string())
