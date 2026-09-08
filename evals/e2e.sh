#!/usr/bin/env bash
# End-to-end evals over every scenario (phase 5): restart the replay on each scenario, wait for
# ground truth, run the watch and report suites, record results. Local stack only.
#   ./evals/e2e.sh [suites]      default: watch,report
set -euo pipefail
cd "$(dirname "$0")/.."
SUITES="${1:-watch,report}"
for f in data/scenarios/*.yaml; do
  echo "== scenario $f"
  SCENARIO_FILE="/app/data/scenarios/$(basename "$f")" docker compose up -d --force-recreate ais-replay >/dev/null
  for i in $(seq 1 60); do
    n=$(curl -s localhost:8000/ground-truth | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
    [ "$n" -gt 0 ] && break; sleep 2
  done
  uv run -q --python 3.12 --with httpx==0.28.1 --with pyyaml==6.0.3 --with boto3==1.43.86 python evals/node_evals.py --api http://localhost:8000 --suites "$SUITES" "${@:2}"
done
