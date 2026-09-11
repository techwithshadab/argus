# Deployed state, as read from AWS — 2026-09-11

Pulled live with `describe-stacks`, `get-template` and `list-guardrails`. This is what any
TrustModel evaluation is evidence *about*, so it belongs next to the report.

## Stacks

| Stack | Status | Created | Last successful update |
|---|---|---|---|
| `argus-agents` | UPDATE_COMPLETE | 2026-09-08 06:57:38Z | **2026-09-11 02:49:10Z** |
| `argus-data` | UPDATE_COMPLETE | 2026-09-08 05:57:54Z | 2026-09-11 02:48:15Z |
| `argus-platform` | UPDATE_COMPLETE | 2026-09-08 06:46:41Z | 2026-09-10 22:12:11Z |
| `argus-network` | CREATE_COMPLETE | 2026-09-08 05:55:13Z | — |
| `argus-data-AuroraRotationSingleUser…` | CREATE_COMPLETE | 2026-09-08 05:58:02Z | 2026-09-08 05:58:30Z |

**Last successful deployment: 2026-09-11 02:49:10Z**, all stacks `*_COMPLETE`, none in
`ROLLBACK` or `UPDATE_IN_PROGRESS`. That deploy is the one carrying the nine production fixes
(UUID job ids, sensor vocabulary, schema-echo, IPv4 pinning, X-Argus-Public, requeue marker).

## AgentCore resources (argus-agents outputs)

| Resource | Identifier |
|---|---|
| Tools gateway | `argus-tools-a1x48d3pow.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` |
| Agents gateway (A2A) | `argus-agents-an6irzu4ww.gateway.bedrock-agentcore.us-east-1.amazonaws.com` |
| Orchestrator runtime | `runtime/argus_orchestrator-c2ghAE4uSS` |
| Tasking harness (pilot) | `harness/argus_tasking-ommdFdFeif` |
| Orchestrator bundle (pilot) | `configuration-bundle/argus_orchestrator-17b7pA626d` |
| Agent Registry | `NDFjL0bfqLnby06V` |
| Memory | `argus_vessel_memory-ut34JhGnd7` |
| Online evaluations | `argus_watch-0ChYYQDzV8`, `argus_investigator-y472NIEMxJ`, `argus_tasking-27u2OeAt26`, `argus_orchestrator-u5BCsP3tsz` |

Note AgentCore already runs its **own** evaluators against all four agents. TrustModel is a
second, independent opinion — not a replacement.

## Models in production

Bedrock-only, Amazon Nova only (ADR-0002; the IAM vendor allowlist is `amazon` alone). Five
distinct model identities, all `us.` **cross-region inference profiles**:

| Role | Tier | Model id | Used by |
|---|---|---|---|
| fast | `fast` | `us.amazon.nova-lite-v1:0` | Watch sweep triage |
| standard | `standard` | `us.amazon.nova-2-lite-v1:0` | Tasking |
| strong | `strong` | `us.amazon.nova-pro-v1:0` | Investigator branches, report node |
| harness | — | `us.amazon.nova-2-lite-v1:0` | Tasking harness pilot (`harnessModelId`) |
| judge | — | `us.amazon.nova-pro-v1:0` | AgentCore online evaluations (`judgeModelId`) |

Source: `agents/shared/config.py:25-27`, `infra/cdk/cdk.json:35-36`; the deployed
`argus-agents` template resolves to `amazon.nova-2-lite-v1:0` and `amazon.nova-pro-v1:0`.

Tier escalation is live: `model_unavailable` in `shared/models.py` retries the next tier up,
so a single investigation can legitimately show two different models — which is why the trace
records the model **per node**, not once per run.

## Guardrail — resolved

| Name | Id | Version in force | Status | Created |
|---|---|---|---|---|
| `argus-agents` | `oop4nkv1vyo8` | **1** | READY | 2026-09-08 06:57:58Z |

Resolved from the deployed stack resource, which is the authoritative answer because
`BEDROCK_GUARDRAIL_VERSION` is wired as `Fn::GetAtt [GuardrailVersion, Version]`:

```
describe-stack-resource --logical-resource-id GuardrailVersion
  -> AWS::Bedrock::GuardrailVersion  PhysicalId: oop4nkv1vyo8|1  CREATE_COMPLETE
```

`list-guardrails` only ever shows the DRAFT, which is why the earlier read was inconclusive.

**Policy in version 1** (`get-guardrail --guardrail-version 1`, description
`argus-agents policy 412ceb345d`):

| Filter | Input | Output |
|---|---|---|
| PROMPT_ATTACK | LOW | NONE |
| MISCONDUCT | LOW | NONE |
| VIOLENCE | MEDIUM | MEDIUM |
| HATE / SEXUAL / INSULTS | HIGH | HIGH |

Denied topics: **none**. PII entities: **0** (masking deliberately off).

This matches the calibration documented in CLAUDE.md exactly — PROMPT_ATTACK at LOW because
prompts are machine-built and HIGH blocked ~3% of ordinary calls; no personal-surveillance
topic because the classifier blocked ~20% of live Investigator prompts; PII masking off
because personal data is already encrypted at rest. Independently verified against the running
deployment, not taken from the docs.

**No drift:** DRAFT and version 1 share an `updatedAt` to the millisecond
(`…785946Z` vs `…785960Z`), so version 1 is a faithful snapshot. Safe to quote.

---

# Does report generation need to reach the agents or MCP endpoints?

**No. Nothing in the integration calls an agent or an MCP server — ever.**

Verified two ways:

1. **Static** — no `httpx`, `requests`, `urllib` or agent/MCP URL appears anywhere in
   `services/trustmodel/`. The only outbound connections in the whole package are psycopg
   (database) and the TrustModel SDK in step 4.
2. **Empirical** — steps 1–3 were run with `socket.socket` and `socket.create_connection`
   monkeypatched to raise. The trace built, the validation ran and all 24 tools scanned with
   **every socket blocked**.

## How it works without touching them

The agents already wrote down what they did; the integration reads the record, not the runtime.

| Evidence needed | Where it comes from | Why no live call |
|---|---|---|
| Which model, tier, attempts | `investigations.manifest` → `nodes[]` | The orchestrator records it at persist time (`provenance.py:43`) |
| Which prompt version | same manifest — `prompt_hash`, `prompt_version` | Hashed from the image at run time (ADR-0012) |
| Which tools were called | `report.evidence[].source`, dotted `server.tool` | The report cites every claim by tool |
| MCP server versions | `manifest.mcp_servers` | Read from each `/health` **during the run**, not now |
| Who did what, when | `audit_events` (append-only by trigger) | Written as the run happened |
| Tool surface to scan | `mcp-servers/tools.json` | The same file that generates the Cedar policies and registry records |

This is deliberate, and it is the stronger design for an audit:

- **Evidence beats interrogation.** A live probe tells you how the system behaves *now*; the
  manifest tells you what actually produced *this* report. For governance, the second is the
  question being asked.
- **The agents are unreachable anyway.** They run in an isolated subnet group with no NAT
  route (ADR-0007). Anything requiring a live agent call would have to breach that.
- **Read-only, replayable.** Export a trace once and every downstream check can be re-run
  offline, months later, without the stack running at all.

## The two things that *would* need live access

| Capability | Why it needs reaching in | Status |
|---|---|---|
| **Red Team** (Prompt Defense, LLM01/LLM07) | 500 adversarial probes must hit a live endpoint | ❌ not scoped — needs your explicit go-ahead |
| **Direct Nova model scan** | TrustModel must call the model itself | ❌ needs a `/v1/chat/completions` shim; breaches ADR-0007 isolation |

Everything else — agent trajectory, tool plane, prompts-as-attested, responses, model
attribution, human-in-the-loop evidence — is produced from stored data alone.

## Guardrail in the trace: wired, not yet populated

`metadata.guardrail` is in the exporter and reads `{"recorded": false}` today, because the
manifest does not carry the guardrail. The orchestrator records `guardrail_blocked=True` when
a block occurs (`agents/orchestrator/app.py:505-513`) but never the id or version.

It is deliberately reported as **not recorded** rather than filled from the live deployment:
the trace must attest to what guarded *that* run, and a lookup now would report today's
configuration. A plausible-looking default in an audit artefact is worse than an honest blank.

To populate it, `provenance.manifest()` would add `{"guardrail": {"id", "version",
"policy_hash"}}` from `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION`, which the runtimes
already hold as environment variables. Roughly three lines — but it is **agent-side and needs
a deploy**, so it is not done unasked. Until then the verified values live in this document:
`oop4nkv1vyo8` version **1**, policy `412ceb345d`.

---

# Are the Nova models being scanned?

Short answer: **they are identified, not assessed.** Two different TrustModel products.

| | What it means | Status |
|---|---|---|
| **Model attribution** (in the agent trace) | Every span records which Nova model, which tier, how many attempts, which prompt hash. An auditor can see `nova-2-lite` wrote the tasking and `nova-pro` wrote the report | ✅ **built and verified** |
| **Model evaluation** (SKU 1, `POST /sdk/v1/evaluate/`) | TrustModel sends its own prompt sets *at the model* and scores its behaviour across the dimensions | ❌ **not done** — see blockers |

So the agent evaluation tells you *how Argus used Nova*. It does not tell you *how safe Nova
is*. If your governance question is "is this model fit for maritime intelligence work", the
agent trace does not answer it.

## Why a direct model scan is not straightforward here

1. **No endpoint to point it at.** Their model eval takes a `model` name (`gpt-5`,
   `gemini-2.5-pro`) or a `custom_endpoint` that speaks `/v1/chat/completions`. Argus exposes
   no such endpoint — verified, there is no OpenAI-compatible route anywhere in `services/`.
   Nova is reached through the Bedrock SDK from inside the VPC.
2. **The agents cannot be reached from outside.** They run in an isolated subnet group with
   **no NAT route at all** (ADR-0007); Bedrock is reached over interface endpoints. Nothing
   external can call them.
3. **Cross-region inference profiles.** We use `us.amazon.nova-*`, not bare model ids. Whether
   their catalogue recognises the `us.` prefix is unknown.

## Three ways to actually scan the models

| Option | What it gives | Cost | Deployment impact |
|---|---|---|---|
| **A. Public score lookup** | An existing TrustScore for Nova, if pre-scored | £0 | none |
| **B. Model eval via a shim** | A real scored evaluation of the exact Nova tiers we run | ~1 credit per dimension set | a small `/v1/chat/completions` → Bedrock shim, reachable by them. **New public surface** — I would not build this without your explicit approval |
| **C. Accept attribution only** | The trace already proves which model produced which claim | £0 | none |

### Option A: tried, and the answer is no

Queried live on 2026-09-11 (unauthenticated, free). The public API exposes four score types —
**`mcp`, `chrome`, `cots`, `model`** — and returns `System not found` for every Nova spelling
under both `cots/` and `model/`:

```
GET /v1/public/score/model/amazon.nova-pro-v1:0      -> System not found
GET /v1/public/score/model/us.amazon.nova-pro-v1:0   -> System not found
GET /v1/public/score/model/nova-pro | nova | amazon-nova-pro -> System not found
GET /v1/public/score/cots/{nova,amazon-nova,bedrock,amazon} -> System not found
```

So **there is no off-the-shelf TrustScore for Amazon Nova**. Their 281 pre-scored systems do
not include it. A model-level score therefore requires option B — us standing up an endpoint
and paying for the evaluation.

Note the `mcp` score type: their public API scores **MCP servers** as first-class systems.
Worth checking whether ours could be submitted for a public MCP score once the key is in hand,
since that is the plane we care most about and our local scan already covers the same ground.

**My recommendation: C as the baseline, B only if an auditor specifically demands a
model-level score.** Option B punches a hole through the isolation that ADR-0007 exists to
maintain, which is a significant trade for a score on a model AWS already publishes safety
documentation for — and Bedrock Guardrails already sit in front of every call.

## What I would add to the trace regardless

`InvocationsIntervened` by `GuardrailPolicyType` from CloudWatch, as span metadata. Read-only,
no deployment change, and it turns "a guardrail is attached" into "the guardrail fired N times
on this run" — which is real safety evidence rather than a configuration claim.
