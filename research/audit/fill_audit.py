"""Fill-level replay audit.

Replays a strategy over every session and records each fill (callback batch, fill batch, symbol, side,
qty, price, liquidity, fee, penalty, client id) together with the mid at fill batch + 120 (the corrected
60 s mark) and, for comparison, the mid the original sweep convention would have used.

    AUDIT_DATA=data python research/audit/fill_audit.py strategies/yl_touch.py out/touch.json
"""
import sys, os, json, importlib.util
from multiprocessing import Pool
H = 120
DATA = os.environ.get("AUDIT_DATA")

def load(path):
    from pqt_client.v2 import Strategy
    spec = importlib.util.spec_from_file_location("strat", path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return next(v for v in vars(m).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")

def run_one(args):
    path, name = args
    from pqt_client.backtest_v2 import load_session
    from pqt_client.v2 import backtest_strategy
    base = load(path); log = []
    class W(base):
        _b = 0
        def on_batch(self, state):
            self._b = state.batch; return super().on_batch(state)
        def on_fill(self, f):
            log.append((self._b, f.batch, f.symbol, f.side.name, f.qty, f.price, f.liquidity, f.fee_mills, f.penalty_mills, str(f.client_id or "")))
            return super().on_fill(f)
    s = load_session(DATA, name); r = backtest_strategy(s, W()); n = s.n_batches
    mid = lambda t: None if (t.bid_px is None or t.ask_px is None) else (t.bid_px + t.ask_px) / 2
    mkt = {}
    for b in range(n):
        for tr in s.trades[b]:
            mkt[tr.symbol] = mkt.get(tr.symbol, 0) + tr.qty
    rows = []
    for cb, fb, sym, side, q, px, liq, fee, pen, cid in log:
        sg = 1 if side == "BUY" else -1
        j_old = min(cb + H, n - 1); j_new = fb + H
        m_old = mid(s.tops[j_old][sym])
        m_new = mid(s.tops[j_new][sym]) if j_new <= n - 1 else None
        prev = mid(s.tops[fb - 1][sym]) if fb >= 1 else None
        rows.append(dict(cb=cb, fb=fb, sym=sym, sg=sg, q=q, px=px, liq=liq, fee=fee, pen=pen, cid=cid,
                         m_old=m_old, m_new=m_new, trunc=(j_new > n - 1), prev=prev))
    return dict(name=name, n=n, rows=rows, mkt=mkt, bt=r.pnl_est_mills / 1000, fees=r.fees_mills / 1000, pen=r.penalties_mills / 1000)

if __name__ == "__main__":
    path, out = sys.argv[1], sys.argv[2]
    from pqt_client.backtest_v2 import load_manifest
    names = load_manifest(DATA).sessions
    with Pool(4) as p: res = p.map(run_one, [(path, n) for n in names])
    json.dump(res, open(out, "w"))
    print("done", path, len(res), sum(len(r["rows"]) for r in res))
