#!/usr/bin/env bash
# Delete the whole AWS stack through CDK, then garbage-collect the container images and file assets
# that `cdk deploy` uploaded to the bootstrap bucket and ECR repository (cdk destroy never touches
# those). The bootstrap stack itself (CDKToolkit) is left in place; remove it by hand if the account
# will never run CDK again.
set -euo pipefail
cd "$(dirname "$0")/../infra/cdk"
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1
. .venv/bin/activate 2>/dev/null || true
command -v cdk >/dev/null || npm install -g aws-cdk@2

# The stacks keep their data by default (-c retainData / retainArchive default to true),
# so a destroy has to say that it is deleting the records. KEEP_DATA=1 leaves the cluster
# and the archive bucket behind; the default takes a manual snapshot first, so a destroy
# run by mistake is recoverable.
KEEP_DATA="${KEEP_DATA:-0}"
CLUSTER_ID="$(aws rds describe-db-clusters \
  --query "DBClusters[?starts_with(DBClusterIdentifier, 'argus')].DBClusterIdentifier | [0]" \
  --output text 2>/dev/null || echo None)"

if [ "$KEEP_DATA" = "1" ]; then
  echo "KEEP_DATA=1: the database and the archive bucket are retained."
  RETAIN_ARGS=(-c retainData=true -c retainArchive=true)
else
  if [ "$CLUSTER_ID" != "None" ] && [ -n "$CLUSTER_ID" ]; then
    SNAP="argus-predestroy-$(date -u +%Y%m%d-%H%M%S)"
    echo "Snapshotting $CLUSTER_ID as $SNAP before deleting it."
    aws rds create-db-cluster-snapshot \
      --db-cluster-identifier "$CLUSTER_ID" --db-cluster-snapshot-identifier "$SNAP" >/dev/null
    aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier "$SNAP"
    echo "Snapshot $SNAP is available; restore with docs/RUNBOOK.md."
  fi
  echo "Deleting the database and the archive bucket. Ctrl-C now to stop."
  RETAIN_ARGS=(-c retainData=false -c retainArchive=false)
fi

cdk destroy --all --force "${RETAIN_ARGS[@]}"
# AgentCore releases its managed network interfaces asynchronously, and while they
# exist the VPC cannot be deleted: a previous destroy left one pinned for days (I6).
VPC=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=argus-network/Vpc" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo None)
if [ "$VPC" != "None" ] && [ -n "$VPC" ]; then
  for _ in $(seq 1 30); do
    N=$(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$VPC" \
      --query 'length(NetworkInterfaces)' --output text 2>/dev/null || echo 0)
    [ "$N" = "0" ] && break
    echo "waiting for $N network interfaces in $VPC to drain"
    sleep 20
  done
fi

cdk gc "aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION}" --unstable=gc --confirm=false --rollback-buffer-days=0
echo "Destroyed all Argus stacks and cleaned bootstrap assets."

# Reported, never deleted: `aws/spans` and the Application Signals groups are
# account-level and may belong to another workload, and a retained guardrail version is
# retained on purpose (deleting one while a runtime still referenced it failed every
# model call). This tells the operator what to look at, and stops nothing.
echo
echo "Leftovers to check by hand:"
for prefix in /aws/bedrock-agentcore/runtimes /aws/ecs/containerinsights aws/spans /aws/application-signals; do
  aws logs describe-log-groups --log-group-name-prefix "$prefix" \
    --query 'logGroups[].logGroupName' --output text 2>/dev/null || true
done
aws bedrock list-guardrails \
  --query "guardrails[?contains(name,'argus')].[name,version]" --output text 2>/dev/null || true
if [ "$VPC" != "None" ] && [ -n "$VPC" ]; then
  echo "VPC $VPC (delete once its network interfaces are gone)"
fi
