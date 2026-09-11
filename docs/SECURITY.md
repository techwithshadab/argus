# Argus security and compliance

## Threat model

Argus handles operational findings about vessels, beneficial-owner names (personal data), and sanctions text. Threats considered: data leaving the account through model calls; one agent impersonating another or reaching tools it should not; prompt injection through tool outputs (registry notes, OSM names, sanctions captions); an agent recommending or attempting an action outside its mandate; loss of the ability to reproduce a finding; unauthorised access to the watch floor.

## Controls

| Concern | Control | Where |
|---|---|---|
| Data egress to models | Bedrock only in production via a VPC endpoint; synth-time guard refuses other providers; only the `amazon` vendor grantable in IAM | ADR-0002, `infra/cdk/stacks/lifecycle.py` |
| Agent identity | One IAM execution role per agent; orchestrator alone may invoke specialists and use memory | ADR-0001, `agents_stack.py` |
| Tool authorization | On AWS every tool call passes the AgentCore Gateway (IAM inbound) and its Policy engine: one Cedar policy per agent role, default deny, forbid wins, every decision logged; tool runtimes accept the gateway role only (resource policy). Agent-only API routes still verify the caller's IAM role through STS-signed tokens (ADR-0001, ADR-0011) |
| Network | Agents in isolated subnets with no NAT route; interface endpoints for every AWS service; security groups scoped to ports; flow logs on; a NAT gateway per zone; the public ALB HTTPS only, limited to `uiAllowedCidr`, behind AWS WAF (rate limit, IP reputation, common exploits, known bad inputs) | ADR-0007, ADR-0018, `network_stack.py`, `edge.py` |
| Watch-floor identity | Cognito user pool (`argus-officers`, operator-created accounts, MFA optional or required) with the ALB's authenticate action on the UI and Grafana listeners; the API verifies the balancer's signed id token (key, signer, issuer) and records the email; non-browser callers sign with IAM through `argus-operator` | ADR-0018, `services/api/officerauth.py` |
| Prompt injection | External free text marked `untrusted` and capped by the tool servers; every prompt states tool output is data; report checked for allowed actions and evidence traceability | `common/safety.py`, `shared/policy.py` |
| Human gate | Actions are `proposed` until a watch officer approves; findings carry a review state and show as AI drafts | API, UI |
| Personal data | `beneficial_owner` and person names stored only encrypted (pgcrypto); key in KMS-backed Secrets Manager; decrypted only by the registry tool; UI and API stay pseudonymous | `datakey.py`, `003/004` SQL |
| Encryption at rest and in transit | Aurora and S3 archive KMS-encrypted; SQS KMS-encrypted; SNS publishers must use TLS; both load balancers terminate TLS (a domain certificate, else one generated at deploy time inside a Lambda and imported into ACM; agents trust the internal one through SSM); TLS to AWS endpoints | `data_stack.py`, `platform_stack.py`, `edge.py` |
| Secret rotation | Database password rotated every 30 days by Secrets Manager hosted rotation; every consumer re-reads the secret when a connection fails and rebuilds its pool | `data_stack.py`, `services/api/dbconn.py` |
| Detection | 36 CloudWatch alarms (default configuration) on queues, dead letters, balancers, tasks, Aurora, NAT, WAF, degraded branches and the eval gate, plus the Grafana rules on the API's metrics; all to one SNS topic. Access logs of both balancers and the archive bucket kept 90 days | `alarms.py`, `docs/RUNBOOK.md#alarms` |
| Infrastructure review | cdk-nag AwsSolutions rules on every synth; each remaining finding suppressed with its evidence | `infra/cdk/stacks/nag.py` |
| Auditability | Append-only audit events for every state change with actor and time; provenance manifest and evidence snapshots per investigation | `002`, `004`, `006` SQL |
| Retention | `retention_policy`: positions 90 days hot then Parquet archive; findings, audit, evidence 7 years | `004` SQL, archiver |
| Supply chain | All dependencies exactly pinned; images built from pinned bases; CDK asset garbage collection on destroy | requirements files |
| Secrets | Database password and data key in Secrets Manager; never in env files committed; `.env` gitignored | |
| Feed keys | The OpenSanctions key is an AgentCore Identity API-key credential provider backed by the platform secret; the registry runtime exchanges each invocation's workload access token for the key (`GetResourceApiKey`), so the key never sits in a runtime's environment (ADR-0013) | `mcp-servers/common/identity.py` |
| Agent-to-agent calls | Orchestrator and worker reach the specialist agents through the `argus-agents` AgentCore Gateway (IAM inbound, every call logged) and resolve their addresses from the AWS Agent Registry (ADR-0013) | `agents/shared/discovery.py` |

## Known gaps

- Without a domain certificate the public listener's certificate is self-signed (a browser warning per profile). Supply `uiCertificateArn` for real use; CloudFront in front of the balancer needs that domain first.
- `argus-operator` is assumable by any principal in the account by default; set `operatorPrincipalArn` to the CI or operator principal.
- Deploys run as `argus-deployer` (MFA-only administrator role, network stack); the IAM user or Identity Center principal that assumes it, and the credentials for the account's first bootstrap, are created outside the app.
- AgentCore runtimes run in the agent subnets of supported zones only, which in us-east-1 with the default two-zone VPC means a single zone; add a supported second zone to the VPC for availability.
- The data key is re-keyed by an operator (`make rekey-aws`), not on a schedule.

## Retention and deletion

What is kept, for how long, and how it is removed. `retention_policy` in the database is
the machine-readable version of the first two columns.

| Class | Retained | Where it goes | How it is deleted |
|---|---|---|---|
| AIS positions | 90 days hot | Daily partitions exported to Parquet in the archive bucket, then dropped from Aurora; Glacier Instant Retrieval after 30 days, Deep Archive after a year | Bucket lifecycle expires the object after seven years |
| Alerts, investigations, reports | 7 years | Aurora | No automatic deletion; an operator deletes by identifier |
| Evidence snapshots | 7 years | Aurora, frozen at the time of the finding | Deleted with their investigation |
| Audit events | Never pruned | Aurora, append-only by trigger | Not deletable through the application; a database administrator must remove them deliberately, which is the point |
| Personal data (`registry.beneficial_owner_enc`, `entities.name_enc`) | For as long as the registry row exists | Aurora, pgcrypto ciphertext only | Deleting the row deletes the ciphertext; destroying the data key makes every copy unreadable, including copies in snapshots and the archive |
| AgentCore Memory | Per the memory resource's own retention (90 days by default) | Bedrock AgentCore, per vessel | Deleted with the memory resource on `make destroy`; it is derived working context, not the record (ADR-0015) |

Two consequences worth stating plainly, because they surprise people:

**Evidence snapshots outlive the positions they describe.** A snapshot holds the position
count and the points themselves at the moment of the finding, so a report stays
reproducible after the raw partitions have been archived and dropped. That is deliberate:
a finding that cannot be re-read is not auditable. It also means a request to delete a
vessel's raw track does not by itself remove the copies inside snapshots.

**Deleting personal data is a key operation, not a row operation.** Because owner names
exist only as ciphertext, and because copies live in database snapshots and possibly in
the archive, the reliable way to make them unreadable everywhere is to destroy the data
key in Secrets Manager. Re-keying (`make rekey-aws`) re-encrypts the live rows under a new
key, which leaves older snapshots readable only with the old key.

Backups: Aurora automated backups cover seven days; a pre-destroy snapshot is manual and
does not expire. Both contain personal data as ciphertext.

## Data classification

Synthetic scenarios are fictional. When real feeds are enabled: AIS positions are public data; registry and sanctions data may include personal data (protected as above); alerts, investigations and reports are operational records with 7-year retention.
