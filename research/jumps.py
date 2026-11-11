"""Jump-diffusion view of the data: diffusion vol, jump frequency/size, cross-symbol propagation, and how long a jump takes to settle."""
import sys, numpy as np, pandas as pd
data = sys.argv[1]; S = ["HPPO", "BNKY", "CRRT", "BRRY"]
dif, jumps, co, settle = {s: [] for s in S}, {s: [] for s in S}, [], {s: [] for s in S}
for i in range(1, 25):
    t = pd.read_csv(f"{data}/s{i:02d}.top.csv")
    mid = ((t.bid_px + t.ask_px) / 2).groupby([t.batch, t.symbol]).first().unstack()[S].ffill()
    d10 = mid.diff(10)                      # 5 s changes
    for s in S:
        x = d10[s].dropna(); mad = 1.4826 * (x - x.median()).abs().median()
        dif[s].append(mad / np.sqrt(10))    # robust per-batch diffusion sd
        J = x[x.abs() > 6 * mad]
        # count distinct jump episodes (>= 20 batches apart)
        idx = J.index.values; ep = idx[np.r_[True, np.diff(idx) > 20]] if len(idx) else idx
        jumps[s].append((len(ep), x.loc[ep].abs().mean() if len(ep) else np.nan))
        for b in ep:
            if b + 240 in mid.index and b - 10 in mid.index:
                pre, path = mid[s].loc[b - 10], mid[s].loc[b - 10:b + 240].values
                tot = path[-1] - pre
                if abs(tot) > 0:
                    frac = (path - pre) / tot
                    k = np.argmax(np.abs(frac - 1) < 0.2)  # first batch within 20% of the final move
                    settle[s].append(k - 10)
            if s == "BRRY":   # does a BRRY jump move the others in the same 5 s?
                co.append([d10.loc[b, o] / d10.loc[b, "BRRY"] for o in S if o != "BRRY"])
print("symbol | diffusion sd per batch (c) | 60 s diffusion sd (c) | jump episodes/session (>6 robust sd in 5 s) | avg |jump| (c) | median batches to settle")
for s in S:
    sd = np.mean(dif[s]); n = np.mean([a for a, _ in jumps[s]]); sz = np.nanmean([b for _, b in jumps[s]])
    st = np.median(settle[s]) if settle[s] else np.nan
    print(f"{s:5} | {sd:5.2f} | {sd*np.sqrt(120):6.1f} | {n:5.1f} | {sz:6.1f} | {st}")
c = np.array(co)
print("On BRRY jumps, same-5s move of HPPO, BNKY, CRRT per 1c of BRRY:", np.nanmedian(c, axis=0).round(2), "(theory HPPO 2.0)")
