# Access

Where the deployed system is, who signs in, and where each credential lives. Nothing here
is a secret: passwords stay in Cognito, Secrets Manager and your mailbox.

## The deployment

| What | Where |
|---|---|
| Region, account | `us-east-1`, the deploying account |
| Watch floor (UI) | `https://<public ALB DNS>` — output `UiUrl` of the `argus-platform` stack |
| Grafana (board `argus`) | `https://<public ALB DNS>:3000` — output `GrafanaUrl` |
| API for tooling | `https://<public ALB DNS>/api/...` with a caller token (see below) |
| Stack outputs | `aws cloudformation describe-stacks --stack-name argus-platform --query "Stacks[0].Outputs"` |

The public DNS name changes when the platform stack is recreated; the stack outputs are the
source of truth. Operators keep the current values in `docs/ACCESS.local.md`, which stays out of
version control (see `.gitignore`).

The certificate is self-signed unless the stack was deployed with `-c uiCertificateArn`,
so the browser shows a warning once per name and port. Port 80 redirects to 443.

## Officers: the browser sign-in

Every page on the UI and on Grafana goes through the load balancer's Cognito sign-in
(ADR-0018). One session covers both; it lasts eight hours.

| Item | Value |
|---|---|
| Sign-in page | opens automatically (`argus-<account>.auth.us-east-1.amazoncognito.com`) |
| User pool | `argus-officers`, output `OfficerPoolId` |
| Username | your email address; the first officer is the `OFFICER_EMAIL` the stack was deployed with |
| First password | Cognito emails a temporary one (sender `no-reply@verificationemail.com`, valid three days); the first sign-in sets the real one: 12+ characters with upper, lower, digit and symbol |
| MFA | optional by default, so an officer without an authenticator is never locked out; `-c officerMfa=required` enforces TOTP enrolment at the next sign-in |

The **officer** box at the top right of the UI is filled in from the sign-in and is
read-only on AWS: the API records the email from the signed token on every review,
approval and rejection. Locally (`OFFICER_AUTH=header`) it is a free text id you type.

Adding an officer (operator, no self sign-up):

```
aws cognito-idp admin-create-user --region us-east-1 --user-pool-id <OfficerPoolId> \
  --username officer@example.org --user-attributes Name=email,Value=officer@example.org Name=email_verified,Value=true
```

Re-sending a temporary password: the same command with `--message-action RESEND`.

## Grafana's own login

After the Cognito step Grafana shows its own login. User `admin`; the password is
generated per deploy and stored in Secrets Manager (output `GrafanaAdminSecretArn`):

```
aws secretsmanager get-secret-value --region us-east-1 --secret-id <GrafanaAdminSecretArn> --query SecretString --output text
```

Grafana applies that password on every start, so it never drifts from the secret.

## Tooling: the IAM path

Requests to `/api/*` that carry `Authorization: Bearer <caller token>` skip the browser
sign-in at the load balancer and are verified by the API against IAM; only the roles in
`TOOL_ALLOWED_ROLES` are admitted. `make eval-aws` assumes the `argus-operator` role
(output `OperatorRoleArn`) and runs the node evals through this path. Root credentials
cannot assume roles, so use an IAM user or role that may assume `argus-operator`.

## Operators: the AWS side

| Item | Where |
|---|---|
| Deploying without root | assume `argus-deployer` (MFA), see RUNBOOK.md |
| Alarms (31) | CloudWatch, prefix `argus-`; alarm and recovery notify SNS topic `argus-alerts` (subscribe with `-c alertEmail=...`) |
| Agents, gateways, memory, evaluations | Bedrock AgentCore console |
| Database password | rotated monthly by Secrets Manager; personal-data key re-keyed with `make rekey-aws` |
| Feed keys | platform stack secrets (`AisStreamKeyArn`, `OpenSanctionsKeyArn`), written by `scripts/deploy.sh` from `.env` |

## Local stack

`make up` serves the UI on `http://localhost:8088` and Grafana on `http://localhost:3000`
(user `admin`, password `GRAFANA_ADMIN_PASSWORD` from `.env`, default `admin`) with no
sign-in; the officer id is whatever you type.
