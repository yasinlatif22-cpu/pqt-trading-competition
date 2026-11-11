#!/usr/bin/env bash
# Reproduces the replay experiments in docs/validation-and-limitations.md.
# Usage (from the repo root, with the SDK importable and the data pack in ./data):
#   AUDIT_DATA=data bash research/audit/run.sh
set -euo pipefail
OUT=${AUDIT_OUT:-research/audit/out}; mkdir -p "$OUT"
A="python research/audit/fill_audit.py"
$A strategies/yl_mm.py "$OUT/mm.json"
$A strategies/yl_touch.py "$OUT/touch.json"
PQT_FILTER=999 PQT_TAKE=0 $A strategies/yl_alpha.py "$OUT/a0p.json"    # EMA-centred baseline, passive
PQT_FILTER=999 PQT_TAKE=2 $A strategies/yl_alpha.py "$OUT/a0t.json"    # + HPPO taker
PQT_FILTER=2 PQT_TAKE=0 $A strategies/yl_alpha.py "$OUT/f2p.json"      # pull side if |forecast| >= 2c
PQT_FILTER=4 PQT_TAKE=0 $A strategies/yl_alpha.py "$OUT/f4p.json"
PQT_FILTER=6 PQT_TAKE=0 $A strategies/yl_alpha.py "$OUT/f6p.json"
(cd research/audit && python fill_summary.py "../../$OUT" && python fill_extras.py "../../$OUT")
