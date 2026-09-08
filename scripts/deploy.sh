#!/usr/bin/env bash
# Deploy or update the whole AWS stack through CDK. Needs: AWS credentials, Docker (buildx for arm64),
# Node 20+, Python 3.12.
#   ./scripts/deploy.sh                 deploy / update (running)
#   PAUSED=true ./scripts/deploy.sh     stop: compute to zero, Aurora auto-pauses
# Extra CDK context can be passed through CDK_CONTEXT, e.g. CDK_CONTEXT="-c uiAllowedCidr=203.0.113.0/24".
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT/infra/cdk"
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1

python3 -m venv .venv >/dev/null 2>&1 || true
. .venv/bin/activate
pip install -q -r requirements.txt
command -v cdk >/dev/null || npm install -g aws-cdk@2

PAUSED="${PAUSED:-false}"
# External feed keys come from the environment or the repo's .env, never from CDK context, so
# they stay out of every template and log. The stacks own the secrets; this script sets values.
ENV_FILE="$REPO_ROOT/.env"
env_or_dotenv() { # name
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then v="$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//')"; fi
  printf '%s' "$v"
}
AIS_MODE="$(env_or_dotenv AIS_MODE)"; AIS_MODE="${AIS_MODE:-replay}"
WATCH_AREAS="$(env_or_dotenv WATCH_AREAS)"; WATCH_AREAS="${WATCH_AREAS:-all}"
AISSTREAM_API_KEY="$(env_or_dotenv AISSTREAM_API_KEY)"
OPENSANCTIONS_API_KEY="$(env_or_dotenv OPENSANCTIONS_API_KEY)"
OPEN_SANCTIONS=false; [ -n "$OPENSANCTIONS_API_KEY" ] && OPEN_SANCTIONS=true
# The first watch-floor account and its MFA policy (ADR-0018); Cognito emails the temporary password.
OFFICER_EMAIL="$(env_or_dotenv OFFICER_EMAIL)"
OFFICER_MFA="$(env_or_dotenv OFFICER_MFA)"; OFFICER_MFA="${OFFICER_MFA:-optional}"
echo "AIS mode: ${AIS_MODE} (areas ${WATCH_AREAS}) · OpenSanctions: ${OPEN_SANCTIONS} · officer: ${OFFICER_EMAIL:-<none>} (mfa ${OFFICER_MFA})"

set_secret_and_restart() { # output-key value service-name-fragment
  local arn
  arn=$(aws cloudformation describe-stacks --stack-name argus-platform --region "$CDK_DEFAULT_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text)
  aws secretsmanager put-secret-value --secret-id "$arn" --secret-string "$2" --region "$CDK_DEFAULT_REGION" >/dev/null
  local cluster service
  cluster=$(aws ecs list-clusters --region "$CDK_DEFAULT_REGION" --query "clusterArns[?contains(@,'argus-platform')]|[0]" --output text)
  # list-services pages at 10 and a --query is applied per page, so list everything and grep.
  service=$(aws ecs list-services --cluster "$cluster" --region "$CDK_DEFAULT_REGION" --max-items 100 --query "serviceArns[]" --output text | tr '\t' '\n' | grep -m1 "$3" || true)
  if [ -n "$service" ]; then
    aws ecs update-service --cluster "$cluster" --service "$service" --force-new-deployment --region "$CDK_DEFAULT_REGION" >/dev/null
    echo "$1 stored; $3 restarted to pick it up."
  else
    # AgentCore runtimes (the tool servers) read the secret when a session starts: no restart.
    echo "$1 stored; no ECS service matches '$3', runtimes pick it up on their next session."
  fi
}
export GIT_SHA="${GIT_SHA:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
# AgentCore VPC mode supports only some availability zones, listed by zone id in cdk.json
# (agentcoreZoneIds). Zone ids map to different zone names per account, so resolve them here.
if [ -z "${AGENTCORE_AZS:-}" ]; then
  ZONE_IDS=$(python3 -c "import json; print(json.load(open('cdk.json'))['context'].get('agentcoreZoneIds',''))")
  AGENTCORE_AZS=$(aws ec2 describe-availability-zones --region "$CDK_DEFAULT_REGION" \
    --query "AvailabilityZones[?contains('${ZONE_IDS}', ZoneId)].ZoneName" --output text | tr '\t' ',')
fi
echo "AgentCore zones: ${AGENTCORE_AZS:-<all>}"
# shellcheck disable=SC2086
cdk bootstrap "aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION}" -q
# shellcheck disable=SC2086
cdk deploy --all --require-approval never --concurrency 2 -c "paused=${PAUSED}" -c "agentcoreAzs=${AGENTCORE_AZS}" -c "aisMode=${AIS_MODE}" -c "watchAreas=${WATCH_AREAS}" -c "openSanctions=${OPEN_SANCTIONS}" -c "officerEmail=${OFFICER_EMAIL}" -c "officerMfa=${OFFICER_MFA}" ${CDK_CONTEXT:-}

if [ "$PAUSED" != "true" ]; then
  [ "$AIS_MODE" = "live" ] && [ -n "$AISSTREAM_API_KEY" ] && set_secret_and_restart AisStreamKeyArn "$AISSTREAM_API_KEY" aisreplay
  # JSON-shaped: the AgentCore Identity credential provider reads the `api_key` field.
  [ "$OPEN_SANCTIONS" = "true" ] && set_secret_and_restart OpenSanctionsKeyArn "{\"api_key\":\"$OPENSANCTIONS_API_KEY\"}" mcpregistry
fi

echo
if [ "$PAUSED" = "true" ]; then
  echo "Stopped: ECS services at 0 tasks, Aurora auto-pauses after 15 min idle. Run 'make start-aws' to resume."
else
  echo "Deployed. UI URL:"
  aws cloudformation describe-stacks --stack-name argus-platform --query "Stacks[0].Outputs[?OutputKey=='UiUrl'].OutputValue" --output text --region "$CDK_DEFAULT_REGION"
fi
