"""Summarise fill_audit.py output: corrected vs original proxy, window/skip stats, per-symbol capture.

    python research/audit/fill_summary.py out        # summarises the configs listed at the bottom
"""
import json, sys, numpy as np, pandas as pd
H=120; O=sys.argv[1]
def load(n):
    res=json.load(open(f"{O}/{n}.json")); rows=[]
    for r in res:
        for x in r["rows"]: x=dict(x); x["sess"]=int(r["name"][1:]); rows.append(x)
    df=pd.DataFrame(rows); mk=pd.DataFrame([{"sess":int(r["name"][1:]),**{f"mkt_{k}":v for k,v in r["mkt"].items()}} for r in res]).set_index("sess")
    return df, res, mk
def enrich(df):
    df=df.copy()
    df["fee_d"]=df.fee/1000.0; df["pen_d"]=df.pen/1000.0
    df["g_old"]=df.q*df.sg*(df.m_old-df.px)/100.0        # sweep.py: callback batch, clamped to last mid
    df["g_new"]=df.q*df.sg*(df.m_new-df.px)/100.0        # fill batch +120, strict
    df["dist"]=df.sg*(df.prev-df.px)                    # cents better than previous-batch mid (+ = captured)
    return df

SYMS=["HPPO","BNKY","CRRT","BRRY"]
def summarize(name, label=None):
    df,res,mk=load(name); df=enrich(df)
    out={}
    n_sess=len(res)
    tot_q=df.q.sum()
    mism=(df.cb!=df.fb); 
    # sweep.py reproduction
    ok_old=df.m_old.notna()
    gross_old=df[ok_old].g_old.groupby(df[ok_old].sess).sum()
    fees_all=df.fee_d.groupby(df.sess).sum(); pen_all=df.pen_d.groupby(df.sess).sum()
    proxy_old=(gross_old.reindex(range(1,25)).fillna(0)-fees_all.reindex(range(1,25)).fillna(0)-pen_all.reindex(range(1,25)).fillna(0))
    # corrected: fill-batch+H, strict, fees only on included fills; self-trade fills excluded from share counts? keep
    ok_new=df.m_new.notna()
    inc=df[ok_new]
    net_new=(inc.g_new-inc.fee_d-inc.pen_d).groupby(inc.sess).sum().reindex(range(1,25)).fillna(0)
    print(f"\n##### {label or name}")
    print(f"fills {len(df)}  shares {tot_q}  fills/session {len(df)/n_sess:.0f}  mean total proxy[sweep-style] ${proxy_old.mean():,.0f}  corrected ${net_new.mean():,.0f}  (diff ${net_new.mean()-proxy_old.mean():+,.0f}/session)")
    print(f"callback-batch != fill-batch: {mism.mean()*100:.1f}% of fills (cb-fb values: {sorted(df[mism].eval('cb-fb').unique())[:5]})")
    trunc=df.trunc
    print(f"incomplete 60s window (fill within last {H} batches): {trunc.sum()} fills = {df[trunc].q.sum()/tot_q*100:.2f}% of shares; clamped to final mid by sweep.py")
    miss_new=(~trunc)&(~ok_new)
    print(f"missing/one-sided mid at fill+{H}: {miss_new.sum()} fills = {df[miss_new].q.sum()/tot_q*100:.2f}% of shares")
    miss_old=(~ok_old)
    fee_skip=df[miss_old].fee_d.sum()
    print(f"sweep.py skips {miss_old.sum()} fills ({df[miss_old].q.sum()/tot_q*100:.2f}% shares) but still charges their fees: ${fee_skip:,.2f} total")
    print(f"maker share of shares {df[df.liq=='MAKER'].q.sum()/tot_q*100:.0f}%")
    print(f"even/odd mean corrected: odd ${net_new[net_new.index%2==1].mean():,.0f}  even ${net_new[net_new.index%2==0].mean():,.0f}   sd ${net_new.std():,.0f}  neg sessions {(net_new<0).sum()}  SE of mean ${net_new.std()/np.sqrt(len(net_new)):,.0f}")
    # per symbol corrected
    rows=[]
    for s in SYMS:
        d=inc[inc.sym==s]
        if len(d)==0: rows.append((s,0,0,0,0,0,0)); continue
        q=d.q.sum(); gross=d.g_new.sum(); fee=(d.fee_d+d.pen_d).sum()
        mkt=mk[f"mkt_{s}"].sum()
        w=(d.q*d.dist).sum()/q
        rows.append((s,q/n_sess,gross/n_sess,(gross-fee)/n_sess,(gross-fee)/q*100,w,df[df.sym==s].q.sum()/mkt*100))
    print("sym   shares/sess  gross$/sess  net$/sess  net c/sh  avg capture vs prev mid (c)  our share of mkt vol %")
    for r in rows: print(f"{r[0]}  {r[1]:10.0f}  {r[2]:10.1f}  {r[3]:9.1f}  {r[4]:8.2f}  {r[5]:8.2f}  {r[6]:8.1f}")
    return df, net_new
if __name__=="__main__":
    for n,l in [("mm","yl_mm.py (v3, EMA-centred, 2-4c quotes)"),("touch","yl_touch.py (adds touch-joining when spread<=2c)"),
                ("a0p","yl_alpha PASSIVE: EMA-centred (filter=999), no taker"),("m0p","yl_alpha PASSIVE: centred on current mid (alpha 0)"),
                ("m100p","yl_alpha PASSIVE: centred on mid+forecast (alpha 100)"),("f2p","yl_alpha PASSIVE: EMA-centred, pull side when |forecast|>=2c")]:
        summarize(n,l)
