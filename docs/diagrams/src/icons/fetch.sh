#!/usr/bin/env sh
# Refresh the icons used by technical-architecture.html. AWS icons come from the official
# AWS Architecture Icons package (https://aws.amazon.com/architecture/icons/); the link on that
# page changes per release, so pass the current package URL as $1. Product marks come from
# Simple Icons and the projects' own repositories.
set -eu
cd "$(dirname "$0")"
PACK="${1:-}"
for s in grafana prometheus postgresql opentelemetry docker langchain langgraph modelcontextprotocol python fastapi nginx openstreetmap github; do
  curl -sfL -m 30 -o "$s.svg" "https://cdn.simpleicons.org/$s"
done
curl -sfL -m 30 -o loki.png "https://raw.githubusercontent.com/grafana/loki/main/docs/sources/logo.png"
curl -sfL -m 30 -o tempo.png "https://raw.githubusercontent.com/grafana/tempo/main/docs/sources/tempo/logo_and_name.png"
curl -sfL -m 30 -o valkey.svg "https://valkey.io/img/valkey-horizontal.svg"
curl -sfL -m 30 -o strands.png "https://github.com/strands-agents.png?size=128"
curl -sfL -m 30 -o opensanctions.svg "https://assets.opensanctions.org/images/ura/logo_text.svg"
[ -n "$PACK" ] || { echo "AWS icons unchanged (pass the Asset Package URL to refresh them)"; exit 0; }
tmp=$(mktemp -d); curl -sfL -m 600 -o "$tmp/pack.zip" "$PACK"; unzip -q "$tmp/pack.zip" -d "$tmp/pack"
A=$(ls -d "$tmp"/pack/Architecture-Service-Icons_*); R=$(ls -d "$tmp"/pack/Resource-Icons_*); G=$(ls -d "$tmp"/pack/Architecture-Group-Icons_*)
svc() { cp "$A/$1/64/Arch_$2_64.svg" "aws/$3.svg"; }
svc Arch_Artificial-Intelligence Amazon-Bedrock-AgentCore agentcore; svc Arch_Artificial-Intelligence Amazon-Bedrock bedrock; svc Arch_Artificial-Intelligence Amazon-Nova nova
svc Arch_Containers AWS-Fargate fargate; svc Arch_Containers Amazon-Elastic-Container-Service ecs; svc Arch_Containers Amazon-Elastic-Container-Registry ecr
svc Arch_Databases Amazon-Aurora aurora; svc Arch_Databases Amazon-ElastiCache elasticache
svc Arch_Application-Integration Amazon-Simple-Queue-Service sqs; svc Arch_Application-Integration Amazon-EventBridge eventbridge; svc Arch_Application-Integration Amazon-Simple-Notification-Service sns
svc Arch_Security-Identity AWS-Secrets-Manager secretsmanager; svc Arch_Security-Identity AWS-Key-Management-Service kms; svc Arch_Security-Identity AWS-Identity-and-Access-Management iam; svc Arch_Security-Identity AWS-WAF waf; svc Arch_Security-Identity Amazon-Cognito cognito; svc Arch_Security-Identity AWS-Certificate-Manager acm
svc Arch_Storage Amazon-Simple-Storage-Service s3; svc Arch_Management-Tools Amazon-CloudWatch cloudwatch; svc Arch_Management-Tools AWS-Systems-Manager ssm; svc Arch_Management-Tools AWS-CloudFormation cloudformation
svc Arch_Developer-Tools AWS-Cloud-Development-Kit cdk; svc Arch_Developer-Tools AWS-X-Ray xray
svc Arch_Networking-Content-Delivery AWS-PrivateLink privatelink; svc Arch_Networking-Content-Delivery Amazon-Virtual-Private-Cloud vpc; svc Arch_Networking-Content-Delivery Elastic-Load-Balancing elb; svc Arch_Compute AWS-Lambda lambda
res() { cp "$R/$1/Res_$2_48.svg" "aws/$3.svg"; }
res Res_Networking-Content-Delivery Amazon-VPC_NAT-Gateway nat; res Res_Networking-Content-Delivery Amazon-VPC_Endpoints endpoint; res Res_Networking-Content-Delivery Elastic-Load-Balancing_Application-Load-Balancer alb; res Res_Networking-Content-Delivery Amazon-VPC_Flow-Logs flowlogs
res Res_Management-Governance AWS-Systems-Manager_Parameter-Store parameterstore; res Res_Management-Governance Amazon-CloudWatch_Logs cwlogs
res Res_Application-Integration Amazon-Simple-Queue-Service_Queue queue; res Res_Application-Integration Amazon-Simple-Notification-Service_Topic topic
res Res_Containers Amazon-Elastic-Container-Service_Service ecs-service; res Res_Containers Amazon-Elastic-Container-Service_Task ecs-task; res Res_Databases Amazon-Aurora-PostgreSQL-Instance-Alternate aurora-postgres
gen() { cp "$R/Res_General-Icons/Res_48_Dark/Res_$1_48_Dark.svg" "aws/$2.svg"; }
for pair in User:user Users:users Internet:internet Documents:documents Alert:alert Magnifying-Glass:search Shield:shield Toolkit:toolkit Programming-Language:code Gear:gear Servers:servers Globe:globe Client:client Document:document Email:email Camera:camera Question:question Source-Code:sourcecode Folder:folder Git-Repository:git Multimedia:multimedia Recover:recover Tape-storage:tape Data-Stream:datastream Logs:logs Metrics:metrics Credentials:credentials Database:database SSL-padlock:padlock JSON-Script:json Generic-Application:app; do gen "${pair%%:*}" "${pair##*:}"; done
cp "$G/AWS-Cloud-logo_32_Dark.svg" aws/group-aws-cloud-dark.svg; cp "$G/AWS-Cloud_32_Dark.svg" aws/group-aws-cloud.svg; cp "$G/Virtual-private-cloud-VPC_32.svg" aws/group-vpc.svg
cp "$G/Private-subnet_32.svg" aws/group-private-subnet.svg; cp "$G/Public-subnet_32.svg" aws/group-public-subnet.svg; cp "$G/Region_32.svg" aws/group-region.svg
rm -rf "$tmp"; echo "icons refreshed"
