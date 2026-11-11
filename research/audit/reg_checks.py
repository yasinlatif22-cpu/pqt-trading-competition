"""Regression checks: label-lag robustness, single-feature benchmarks, spread/tradability.

    python research/audit/reg_checks.py data
"""
import sys, json, numpy as np, pandas as pd
data = sys.argv[1]; H = 120; SYMS = ["HPPO","BNKY","CRRT","BRRY"]
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

# ---- 1. spread regime in the historical data
sp = {s: [] for s in SYMS}
for i in range(1,25):
    t = pd.read_csv(f"{data}/s{i:02d}.top.csv"); t = t.dropna(subset=["bid_px","ask_px"]); t["sp"] = t.ask_px - t.bid_px
    for s in SYMS: sp[s].append(t[t.symbol==s].sp.values)
print("== SPREAD (cents), 24 historical sessions pooled; per-session median range")
for s in SYMS:
    a = np.concatenate(sp[s]); meds = [np.median(x) for x in sp[s]]
    print(f"{s}: median {np.median(a):.1f}  p10 {np.percentile(a,10):.0f}  p90 {np.percentile(a,90):.0f}  share<=1c {np.mean(a<=1)*100:.1f}%  share<=2c {np.mean(a<=2)*100:.1f}%  session-median range {min(meds):.0f}-{max(meds):.0f}")

# ---- 2. regression reproduction + robustness
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
print("\n== OOS R2 (H=120) by split: odd->even | even->odd | s01-12->s13-24 | s13-24->s01-12 | odd->even vs TRAIN mean")
for s in SYMS:
    r = [fit_eval(odd,even,s), fit_eval(even,odd,s), fit_eval(first,second,s), fit_eval(second,first,s)]
    print(f"{s}: {r[0][0]:.3f} | {r[1][0]:.3f} | {r[2][0]:.3f} | {r[3][0]:.3f} | train-mean baseline {r[0][1]:.3f}")

print("\n== Session-level bootstrap of odd->even pooled test R2 (resample the 12 test sessions; fixed beta) and per-session R2 range")
for s in SYMS:
    r2, _, b = fit_eval(odd, even, s, ret_pred=True)
    per = {k: g for k, g in b.groupby("sess")}
    def r2f(gs):
        g = pd.concat(gs); return 1-((g.y-g.pred)**2).sum()/((g.y-g.y.mean())**2).sum()
    bs = [r2f([per[k] for k in rng.choice(list(per), len(per))]) for _ in range(500)]
    pr = [1-((g.y-g.pred)**2).sum()/((g.y-g.y.mean())**2).sum() for g in per.values()]
    pc = [np.corrcoef(g.pred, g.y)[0,1] for g in per.values()]
    print(f"{s}: R2 {r2:.3f}  session-bootstrap 90% [{np.percentile(bs,5):.3f}, {np.percentile(bs,95):.3f}]  per-session R2 min/med/max {min(pr):.3f}/{np.median(pr):.3f}/{max(pr):.3f}  per-session corr>0 in {sum(c>0 for c in pc)}/{len(pc)}")

print("\n== Benchmarks, odd->even OOS R2: all features | dev_ema60 only | basket_gap only | ret20 only")
for s in SYMS:
    cols_all=None
    vals=[fit_eval(odd,even,s)[0], fit_eval(odd,even,s,cols=["dev_ema60"])[0], fit_eval(odd,even,s,cols=["basket_gap"])[0], fit_eval(odd,even,s,cols=["ret20"])[0]]
    print(f"{s}: " + " | ".join(f"{v:.3f}" for v in vals))

print("\n== Label-start lag test (odd->even R2 when the label starts L batches after the features): L=0,1,5,10,20")
for s in SYMS:
    print(f"{s}: " + " | ".join(f"{fit_eval(odd,even,s,lag=L)[0]:.3f}" for L in (0,1,5,10,20)))

print("\n== Tradability: cross-the-spread net gain (cents/share after 0.4c taker fee) when |pred| > half-spread + 0.4c + margin; odd->even")
for s in SYMS:
    r2, _, b = fit_eval(odd, even, s, ret_pred=True)
    b = b.dropna(subset=["ask","bid"])
    for margin in (0, 1, 2):
        thr = b.spread/2 + 0.4 + margin
        long = b.pred > thr; short = b.pred < -thr
        gain = np.where(long, b.mid_fwd - b.ask - 0.4, np.where(short, b.bid - b.mid_fwd - 0.4, np.nan))
        g = pd.Series(gain, index=b.index).dropna()
        sess = pd.Series(gain, index=b.index).groupby(b.sess).mean()
        n = len(g)
        print(f"{s} margin {margin}c: n_signals {n} ({n/len(b)*100:.2f}% of batches)  mean net {g.mean() if n else float('nan'):+.2f} c/sh  hit {np.mean(g>0)*100 if n else float('nan'):.0f}%  sessions positive {int((sess>0).sum())}/{sess.notna().sum()}")
