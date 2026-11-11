"""Run labelled configs through sweep.py; report train (odd sessions) and test (even) means.

    python grid.py STRATEGY.py DATA_DIR configs.json

configs.json: [{"label": "...", "cfg": {"BRRY": {"half": 5}}, "fair": 60}, ...]
"""
import json
import os
import subprocess
import sys

strat, data, cfgfile = sys.argv[1:4]
configs = json.load(open(cfgfile))
here = os.path.dirname(os.path.abspath(__file__))
print(f"{'label':34} {'train':>8} {'test':>8} {'all':>8} {'neg':>4} {'worst':>7} {'peakE':>6}  by_sym")
for c in configs:
    env = dict(os.environ)
    env["PQT_CFG"] = json.dumps(c.get("cfg", {}))
    if "fair" in c:
        env["PQT_FAIR"] = str(c["fair"])
    for k, v in c.get("env", {}).items():
        env[k] = str(v)
    p = subprocess.run([sys.executable, "-I", os.path.join(here, "sweep.py"), strat, data, "--json"],
                       capture_output=True, text=True, env=env)
    line = [l for l in p.stdout.splitlines() if l.startswith("{")]
    if not line:
        print(c["label"], "FAILED", p.stderr[-300:])
        continue
    o = json.loads(line[-1])
    per = o["per"]
    tr = [v for k, v in per.items() if int(k[1:]) % 2 == 1]
    te = [v for k, v in per.items() if int(k[1:]) % 2 == 0]
    print(f"{c['label']:34} {sum(tr)/len(tr):8.0f} {sum(te)/len(te):8.0f} {o['mean']:8.0f} "
          f"{o['neg']:4d} {o['worst']:7.0f} {o['peakE']:6.0f}  {o['by_sym']}  rej={o['rejects']}", flush=True)
