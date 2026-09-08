#!/usr/bin/env bash
# Scripted demo path against a local stack.
set -euo pipefail
API=${API_URL:-http://localhost:8000}
echo "1) vessels on the plot:";        curl -s $API/vessels | python3 -c "import sys,json; [print(f\"   {v['mmsi']} {v['name']} {v['sog']} kn\") for v in json.load(sys.stdin)]"
echo "2) watch sweep (12 h):";         curl -s -X POST "$API/sweep?hours=12"; echo
echo "   waiting 90 s for alerts...";  sleep 90
curl -s $API/alerts | python3 -c "import sys,json; [print(f\"   {a['severity']:6} {a['kind']:14} {a['mmsi']}  {a['details'].get('rationale','')[:90]}\") for a in json.load(sys.stdin)]"
echo "3) investigate MERIDIAN STAR:";  curl -s -X POST $API/investigations/511666006 -H 'Content-Type: application/json' -d '{"trigger":"demo"}'; echo
echo "   open http://localhost:8088 and watch the report panel; traces in Grafana http://localhost:3000"
