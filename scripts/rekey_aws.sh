#!/usr/bin/env bash
# Re-key the personal-data columns on AWS: one-off ECS task on the API image, same network and
# roles as the API service (it may read and write the data-key secret). Prints the task's log.
set -euo pipefail
REGION=${AWS_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}
CLUSTER=$(aws ecs list-clusters --region "$REGION" --query "clusterArns[?contains(@,'argus-platform')]|[0]" --output text)
SERVICE=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" --max-items 100 --query "serviceArns[]" --output text | tr '\t' '\n' | grep -m1 Svcapi)
read -r TASKDEF NETCFG < <(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION" \
  --query "services[0].[taskDefinition, networkConfiguration]" --output json | python3 -c "import sys,json; t,n=json.load(sys.stdin); print(t, json.dumps(n).replace(' ',''))")
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE --region "$REGION" \
  --network-configuration "$NETCFG" --overrides '{"containerOverrides":[{"name":"app","command":["python","rekey.py"]}]}' \
  --query "tasks[0].taskArn" --output text)
echo "rekey task $TASK"; aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK" --region "$REGION"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --region "$REGION" --query "tasks[0].containers[0].[exitCode,reason]" --output text
aws logs tail /argus/services --region "$REGION" --since 15m --filter-pattern rewrote | tail -5 || true
