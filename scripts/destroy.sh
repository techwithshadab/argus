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

cdk destroy --all --force
cdk gc "aws://${CDK_DEFAULT_ACCOUNT}/${CDK_DEFAULT_REGION}" --unstable=gc --confirm=false --rollback-buffer-days=0
echo "Destroyed all Argus stacks and cleaned bootstrap assets."
