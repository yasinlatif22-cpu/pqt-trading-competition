"""Evaluate a strategy over all sessions in parallel with the 60 s markout proxy.

    python sweep.py STRATEGY.py DATA_DIR [--sessions s01,s02] [--json]

Strategy overrides come from the environment (e.g. PQT_CFG), read by the strategy at import.
Prints one summary line plus per-symbol mean gross markout and peak |net exposure|.
"""
import importlib.util
import json
import sys
from multiprocessing import Pool

H = 120


def run_one(args):
    strat_path, data, name = args
    from pqt_client.backtest_v2 import load_session
    from pqt_client.v2 import Strategy, backtest_strategy

    spec = importlib.util.spec_from_file_location("strat", strat_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = next(v for v in vars(mod).values()
                if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy
                and v.__module__ == "strat")
    log = []
    peak = {"E": 0.0, "msgs": 0}

    class W(base):
        _b = 0

        def on_batch(self, state):
            self._b = state.batch
            e = abs(state.exposure) / 1000.0
            peak["E"] = max(peak["E"], e)
            return super().on_batch(state)

        def on_fill(self, fill):
            log.append((self._b, fill))
            return super().on_fill(fill)

    s = load_session(data, name)
    r = backtest_strategy(s, W())
    gross = 0.0
    by = {}
    for b, f in log:
        q = f.qty if f.side.name == "BUY" else -f.qty
        top = s.tops[min(b + H, len(s.tops) - 1)][f.symbol]
        if top.bid_px is None or top.ask_px is None:
            continue
        m = (top.bid_px + top.ask_px) / 2
        g = q * (m - f.price) / 100.0
        gross += g
        by[f.symbol] = by.get(f.symbol, 0.0) + g
    fees = r.fees_mills / 1000.0
    pen = r.penalties_mills / 1000.0
    rej = r.rejects
    nrej = len(rej) if hasattr(rej, "__len__") else int(rej)
    return dict(name=name, proxy=gross - fees - pen, bt=r.pnl_est_mills / 1000.0,
                fills=len(log), by=by, fees=fees, pen=pen, peakE=peak["E"], rejects=nrej,
                shares=r.shares_traded)


def main():
    strat, data = sys.argv[1], sys.argv[2]
    names = None
    as_json = "--json" in sys.argv
    for a in sys.argv[3:]:
        if a.startswith("--sessions="):
            names = a.split("=", 1)[1].split(",")
    from pqt_client.backtest_v2 import load_manifest
    names = names or load_manifest(data).sessions
    with Pool(min(len(names), 8)) as p:
        res = p.map(run_one, [(strat, data, n) for n in names])
    res.sort(key=lambda r: r["name"])
    vals = sorted(r["proxy"] for r in res)
    n = len(res)
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
    syms = sorted({k for r in res for k in r["by"]})
    out = dict(
        mean=round(mean, 1), sd=round(sd, 1), neg=sum(1 for v in vals if v < 0), n=n,
        worst=round(vals[0], 1), median=round(vals[n // 2], 1),
        by_sym={s: round(sum(r["by"].get(s, 0.0) for r in res) / n, 1) for s in syms},
        peakE=round(max(r["peakE"] for r in res), 0),
        rejects=sum(r["rejects"] for r in res),
        fees=round(sum(r["fees"] for r in res) / n, 1),
        fills=round(sum(r["fills"] for r in res) / n),
        per={r["name"]: round(r["proxy"], 1) for r in res},
    )
    if as_json:
        print(json.dumps(out))
    else:
        out.pop("per")
        for r in res:
            print(r["name"], round(r["proxy"], 1), "| bt", round(r["bt"], 1), "| fills", r["fills"],
                  "| peakE", round(r["peakE"]), "| rej", r["rejects"])
        print(out)


if __name__ == "__main__":
    main()
