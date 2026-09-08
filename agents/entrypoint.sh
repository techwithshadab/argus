#!/bin/sh
# On AgentCore Runtime, AGENT_OBSERVABILITY_ENABLED=true makes the ADOT distro auto-instrument the
# process and ship spans/metrics/logs to CloudWatch (Transaction Search). Locally we export OTLP to
# the collector from code, so we run plain python.
if [ "${AGENT_OBSERVABILITY_ENABLED:-false}" = "true" ]; then
  exec opentelemetry-instrument python app.py
else
  exec python app.py
fi
