"""Feature builder and odd/even forecast fit used by fill_extras.py (reads AUDIT_DATA)."""
import os, sys, json, numpy as np, pandas as pd
data = os.environ['AUDIT_DATA']; H = 120; SYMS = ["HPPO","BNKY","CRRT","BRRY"]
rng = np.random.default_rng(0)

def build(name, lag=0):
    t = pd.read_csv(f"{data}/{name}.top.csv"); tr = pd.read_csv(f"{data}/{name}.trades.csv")
    piv = lambda c: t.pivot(index="batch", columns="symbol", values=c)[SYMS]
    bid, ask, bq, aq = piv("bid_px"), piv("ask_px"), piv("bid_qty"), piv("ask_qty")
    mid = ((bid + ask) / 2).ffill(); micro = ((bid*aq + ask*bq)/(bq+aq)).ffill(); imb = ((bq-aq)/(bq+aq)).fillna(0)
    tr["signed"] = np.where(tr.aggressor == "BUY", 1, -1) * tr.qty
    flow = tr.pivot_table(index="batch", columns="symbol", values="signed", aggfunc="sum").reindex(index=mid.index, columns=SYMS).fillna(0)
    out = {}
    for s in SYMS:
        f = pd.DataFrame(index=mid.index)
        f["micro_dev"] = micro[s]-mid[s]; f["imb"] = imb[s]
        f["flow10"] = flow[s].rolling(10, min_periods=1).sum(); f["flow60"] = flow[s].rolling(60, min_periods=1).sum()
        f["ret20"] = mid[s].diff(20); f["ret120"] = mid[s].diff(120)
        f["dev_ema60"] = mid[s]-mid[s].ewm(span=60).mean(); f["dev_ema240"] = mid[s]-mid[s].ewm(span=240).mean()
        gap = mid.HPPO-mid.BNKY-2*mid.BRRY
        f["basket_gap"] = gap if s=="HPPO" else (-gap if s in ("BNKY","BRRY") else 0*gap)
        f["basket_gap_dm"] = f["basket_gap"]-f["basket_gap"].rolling(600, min_periods=60).mean()
        for o in SYMS:
            if o != s: f[f"x_ret20_{o}"] = mid[o].diff(20)
        f["y"] = mid[s].shift(-(H+lag)) - mid[s].shift(-lag)
        f["ask"] = ask[s]; f["bid"] = bid[s]; f["mid"] = mid[s]; f["mid_fwd"] = mid[s].shift(-H)
        f["spread"] = ask[s]-bid[s]; f["sess"] = name
        out[s] = f.iloc[240:-(H+lag)].dropna(subset=[c for c in f.columns if c not in ("ask","bid")])
    return out, bid, ask

cache = {}
def get(i, lag=0):
    k=(i,lag)
    if k not in cache: cache[k] = build(f"s{i:02d}", lag)[0]
    return cache[k]


def fit_eval(train_ids, test_ids, s, lag=0, cols=None, ret_pred=False):
    a = pd.concat([get(i,lag)[s] for i in train_ids]); b = pd.concat([get(i,lag)[s] for i in test_ids])
    X = cols or [c for c in a.columns if c not in ("y","ask","bid","mid","mid_fwd","spread","sess")]
    mu, sd = a[X].mean(), a[X].std().replace(0,1)
    A = np.c_[np.ones(len(a)), ((a[X]-mu)/sd).values]; B = np.c_[np.ones(len(b)), ((b[X]-mu)/sd).values]
    beta,*_ = np.linalg.lstsq(A, a.y.values, rcond=None); pred = B@beta
    r2 = 1-((b.y-pred)**2).sum()/((b.y-b.y.mean())**2).sum()
    r2_trainmean = 1-((b.y-pred)**2).sum()/((b.y-a.y.mean())**2).sum()
    return (r2, r2_trainmean, b.assign(pred=pred)) if ret_pred else (r2, r2_trainmean)

odd = list(range(1,25,2)); even = list(range(2,25,2)); first = list(range(1,13)); second = list(range(13,25))

odd = list(range(1,25,2)); even = list(range(2,25,2))
