#!/bin/sh
# Service Connect advertises both A and AAAA for `api`; this VPC has no IPv6 CIDR, so the
# AAAA (2600:f0f0::2) is unroutable and nginx, which round-robins the addresses it cached at
# start, failed ~15% of /api/* calls with "Network unreachable". Resolve to IPv4 once here and
# pin it, so nginx never sees the AAAA. Waits up to 90s because Service Connect names only
# resolve once the sidecar is up.
set -e
CONF=/etc/nginx/conf.d/default.conf
addr=""
i=0
while [ $i -lt 90 ]; do
  addr=$(getent ahostsv4 api 2>/dev/null | awk 'NR==1{print $1}')
  [ -n "$addr" ] && break
  i=$((i + 1))
  sleep 1
done
if [ -n "$addr" ]; then
  # Rewrite in place by content, not by replacing the inode: `sed -i` renames a temp file
  # over the target, which fails on a bind-mounted config with "Resource busy".
  tmp=$(mktemp)
  sed "s|http://api:8000/|http://${addr}:8000/|" "$CONF" > "$tmp"
  cat "$tmp" > "$CONF"
  rm -f "$tmp"
  echo "pinned api upstream to ${addr}"
else
  # Leaving the bare name makes nginx refuse to start ("host not found in upstream"),
  # which crash-loops the task and trips the deployment circuit breaker. Point at a
  # dead local port instead: nginx starts, /api/* answers 502, the page still serves,
  # and the next task start (or a force-new-deployment) resolves it properly.
  echo "could not resolve api to IPv4 after 90s; upstream parked on 127.0.0.1" >&2
  tmp=$(mktemp)
  sed "s|http://api:8000/|http://127.0.0.1:8000/|" "$CONF" > "$tmp"
  cat "$tmp" > "$CONF"
  rm -f "$tmp"
fi
