"""HPPO-taker increment, exposure at fills, and net markout by agreement with the out-of-sample forecast.

    AUDIT_DATA=data python research/audit/fill_extras.py out
"""
import sys, json, numpy as np, pandas as pd

from fill_summary import load, enrich, SYMS, summarize
import reg_lib as R
O=sys.argv[1]
W={"HPPO":1.0,"BNKY":0.15,"CRRT":-0.29,"BRRY":0.425}
# ---- HPPO taker incremental + exposure crowd-out
for n,l in [("a0t","yl_alpha: passive BNKY/CRRT/BRRY + HPPO forecast TAKER (take_c=2)")]:
    df,net=summarize(n,l)
def expo_stats(name):
    df,res,mk=load(name); df=enrich(df); out=[]
    for sess,g in df.sort_values(["sess","fb"]).groupby("sess"):
        pos={s:0 for s in SYMS}; 
        for r in g.itertuples():
            pos[r.sym]+=r.sg*r.q; out.append((sess,r.fb,abs(sum(W[s]*pos[s] for s in SYMS)),abs(pos["HPPO"]),abs(pos["BNKY"]),abs(pos["CRRT"]),abs(pos["BRRY"])))
    e=pd.DataFrame(out,columns=["sess","fb","E","pH","pB","pC","pR"])
    return e
print("\n##### exposure at fill events (fills only; resting orders not included)")
for n in ["mm","touch","a0p","a0t"]:
    e=expo_stats(n); print(f"{n:6s} |E| at fills: median {e.E.median():.0f} p95 {e.E.quantile(.95):.0f} max {e.E.max():.0f}  share of fills with |E|>420: {(e.E>420).mean()*100:.1f}%  max|HPPO pos| {e.pH.max():.0f}  max|BRRY pos| {e.pR.max():.0f} (cap 300)  max|BNKY| {e.pB.max():.0f} max|CRRT| {e.pC.max():.0f} (cap 500)")
# ---- does the forecast identify adverse passive fills? baseline EMA-centred passive fills, even sessions, OOS forecast
odd=list(range(1,25,2)); even=list(range(2,25,2))
df,res,mk=load("a0p"); df=enrich(df)
print("\n##### Baseline passive fills (a0p, EMA-centred, forecast NOT used in quoting), even sessions: net c/share by agreement with the OOS forecast at decision time")
rows=[]
for s in ["BNKY","CRRT","BRRY"]:
    r2,_,b=R.fit_eval(odd,even,s,ret_pred=True)
    b=b.reset_index().rename(columns={"index":"batch"}) if "batch" not in b.columns else b.reset_index(drop=True)
    b["sess"]=b["sess"].str[1:].astype(int)
    d=df[(df.sym==s)&(df.sess%2==0)&df.m_new.notna()].copy(); d["batch"]=d.fb-1
    m=d.merge(b[["sess","batch","pred"]],on=["sess","batch"],how="inner")
    m["edge"]=m.sg*m.pred; m["net_c"]=(m.g_new-m.fee_d-m.pen_d)*100/m.q
    m["bucket"]=pd.cut(m.edge,[-1e9,-3,-1,0,1,3,1e9],labels=["<-3 (forecast says toxic)","-3..-1","-1..0","0..1","1..3",">3 (forecast agrees)"])
    g=m.groupby("bucket",observed=True).apply(lambda x: pd.Series({"fills":len(x),"shares":x.q.sum(),"net_c_per_sh":(x.net_c*x.q).sum()/x.q.sum(),"capture_c":(x.q*x.dist).sum()/x.q.sum()}))
    print(f"\n{s}: matched {len(m)}/{len(d)} fills; corr(edge, net markout per fill) = {m.edge.corr(m.net_c):+.3f}")
    print(g.round(2).to_string())
