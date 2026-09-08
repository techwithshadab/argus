#!/usr/bin/env bash
# Node evals against the AWS stack: assume the operator role the platform stack created,
# then call the API through the public load balancer with a caller token (ADR-0018).
# Needs the evals' deps (evals/requirements.txt) and credentials that may assume the role.
set -euo pipefail
REGION=${AWS_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}
out() { aws cloudformation describe-stacks --stack-name argus-platform --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
ROLE=$(out OperatorRoleArn)
UI=$(out UiUrl)
[ -n "$ROLE" ] && [ -n "$UI" ] || { echo "argus-platform outputs OperatorRoleArn/UiUrl not found" >&2; exit 1; }
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN < <(
  aws sts assume-role --role-arn "$ROLE" --role-session-name evals --region "$REGION" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_REGION="$REGION"
# EVAL_TLS_VERIFY=false accepts the self-signed watch-floor certificate; set it to true with
# a domain certificate (-c uiCertificateArn).
export EVAL_AUTH=aws-iam EVAL_TLS_VERIFY=${EVAL_TLS_VERIFY:-false}
exec python evals/node_evals.py --api "${UI%/}/api" --region "$REGION" --gate --push "$@"
