# TrustModel coverage for Argus — what gets evaluated, and how

Answers two questions: **what changes in the deployment** (nothing), and **what we can
evaluate, organised the way TrustModel organises it** rather than the way our code happens to
be laid out.

Companion to `docs/trustmodel/PLAN.md` (research) and `services/trustmodel/README.md` (usage).

---

## 1. Deployment impact: none

| Surface | Change | Why |
|---|---|---|
| Agent code (`agents/`) | **none** | Trace is built from data already persisted |
| Prompts (`agents/shared/prompts/*.md`) | **none** | An edit cuts a new immutable Bedrock Prompt version (ADR-0012) and requires `evals/node_evals.py --gate` against a live stack |
| CDK / infra (`infra/`) | **none** | No new resource, no `make deploy` |
| Running ECS services | **none** | No image rebuild, no restart, no rollout |
| Database | **read-only** | `SELECT` on `investigations`, `alerts`, `audit_events`, `tasking_requests` |
| Bedrock / AgentCore | **none** | No model invoked by this integration |
| Network egress | only step 4, only with `--confirm` | Steps 1–3 are fully offline |

Branch `trustmodel`, staged, not merged. The integration is a **read-side observer**: if it
were deleted tomorrow, Argus would behave identically.

The one thing that *would* change the deployment is SKU 3's runtime enforcement
(`guardrails.decide` before a tool call). That is **not** in scope here — it is a Q3 2026
product and would put a synchronous external dependency in front of every tool call. Argus
already enforces least privilege with Cedar in ENFORCE mode on the tools gateway.

---

## 2. What TrustModel evaluates, mapped to Argus

**First, to be unambiguous:** all four agents, all four MCP servers, all 24 tools and all four
prompts are **deployed, live and working**. Nothing in this document says otherwise. Every
"not built" below refers to a file in `services/trustmodel/` that I have not written yet — the
exporter that turns a running agent's stored output into the trace file TrustModel ingests.
Argus is complete; my integration is not.

TrustModel's unit of evaluation is **one agent, one trace, one goal**. Argus has four agents
with genuinely different shapes, so this is four traces, not one. The difference between them
is not whether the agent runs — it is **where each agent's provenance lands in the database**:

| Table | `trace_id` | `manifest` (model, tier, prompt hash, attempts) | Source |
|---|---|---|---|
| `investigations` | ✅ | ✅ JSONB | `001_schema.sql:75`, `006_manifest.sql:2` |
| `audit_events` | ✅ | `manifest_ref` only | `002_review_audit.sql:23-24` |
| `alerts` | ❌ | ❌ | `001_schema.sql:54-66` |

That single asymmetry is the whole reason the Orchestrator exporter exists and the Watch one
does not yet.

### 2a. Agents (SKU 1 — the core of the audit)

| # | Agent | Framework | Goal evaluated against | Trace source | Trajectory | Status |
|---|---|---|---|---|---|---|
| 1 | **Orchestrator** (+ Investigator branches) | LangGraph | "Investigate a dark-vessel alert and produce a VOI report whose every claim cites a tool" | `investigations.manifest` + `report.evidence` + `audit_events` | identity → behaviour → tasking → report | ✅ **built** |
| 2 | **Investigator** (per branch) | LangGraph ReAct | "Answer the identity / behaviour question, citing every claim" | same manifest, one node each | tool calls per branch | ⚠️ **sub-span today** — scoreable separately only if we emit one trace per branch |
| 3 | **Tasking** | Strands | "Decide whether a re-look is warranted and propose a valid AOI for human approval" | `tasking_requests` + manifest tasking node | imagery/geo calls → `proposed` row | ⚠️ **partial** — node captured, but the AOI-rejection path is the interesting case |
| 4 | **Watch** | Strands | "Raise the candidates worth an officer's attention and dismiss the rest, with reasons" | `alerts` + `audit_events` (`sweep.requested` → `alert.raised`×N → `sweep.completed`) | detectors → context tools → raise/dismiss | ⚠️ **exporter not written** — data is there, see below |

**What Watch actually stores** (`agents/watch/app.py:114-125`), which is more than I credited
in my first pass: every raised alert carries `mmsi`, `kind`, `severity`, `score`, `rationale`,
the `started_at`/`ended_at` window and `evidence` canonicalised to dotted `server.tool` form.
Dismissals carry a reason. The sweep summary adds raised/dismissed/deferred/candidate counts
and failed detectors. That is a complete, scoreable trajectory **with judgement** — arguably a
cleaner one than the Orchestrator's, because the raise/dismiss decision is explicit.

**What is missing is only the manifest**: `alerts` has no `manifest` and no `trace_id` column,
and the sweep summary records counts but no model id, tier or prompt hash
(`agents/watch/app.py:280-290`). So a Watch trace can be assembled today — it just cannot
state *which model and prompt version produced the judgement*, which is exactly the provenance
an audit wants. Two options, neither of which changes agent behaviour:

- **(a)** Build the exporter now and fill model/tier/prompt from `shared/config.py` defaults,
  labelled in the trace as *inferred, not recorded*. Honest but weaker evidence.
- **(b)** Add a `manifest JSONB` column to `alerts` in a new idempotent `data/sql/011_*.sql`
  and have the Watch tool populate it. Stronger, but it **does** touch agent code and needs a
  deploy — which is why I have not done it unasked.

My earlier table said "not built" without distinguishing these, and without noting how much
Watch already records. That was too blunt.

Watch is also the highest-value thing to evaluate, because it is the only agent whose
judgement is *not* checked by a downstream policy gate — a wrongly dismissed candidate simply
never reaches an officer.

### 2b. Tool plane (SKU 3 semantics, run locally)

| Asset | Count | Checks | Status |
|---|---|---|---|
| MCP servers | 4 (ais, geo, imagery, registry) | tool poisoning, prompt injection in descriptions, over-permissive grants | ✅ **built**, all clean |
| MCP tools | 24 | same three checks, per tool | ✅ **built**, all clean |

Mirrors what TrustModel scans "before an agent registers" a tool. Their hosted version is
Q3 2026; ours runs offline against `mcp-servers/tools.json`, the same file that generates the
Cedar policies and the Agent Registry records — so the scan and the enforcement read one
source of truth.

Heuristics over tool metadata, not a proof of safety.

### 2c. Prompts, memory, responses

| Asset | TrustModel surface | Reality for Argus | Status |
|---|---|---|---|
| **Prompts** — *attested* (4 files) | Provenance / Accountability | All four hashed in `metadata.prompts`, plus per-node `prompt_hash` and `prompt_version` from Bedrock Prompt Management (ADR-0012) | ✅ **built** — verified in the exported trace |
| **Prompts** — *attacked* | Prompt Defense dim; LLM07 probes attempt system-prompt extraction | Needs **Red Team**: 500 adversarial probes against a live endpoint. No trace can produce this | ❌ **not scoped** — needs your go-ahead, see §4 |
| **Responses** (VOI reports) | Accuracy + Faithfulness/Groundedness (LLM09) | Already in the trace: `report.evidence` with dotted `server.tool`, plus `information_gaps` | ✅ **built** (as trace content) |
| **Memory** (AgentCore Memory recall) | No dedicated scanner documented | Appears only as `prior_context_chars` in the manifest — the *recalled text itself* is not persisted | ⚠️ **metadata only** — we can say memory was used, not what it said |
| **Guardrail** (Bedrock) | Safety / Prompt Defense | `InvocationsIntervened` is a CloudWatch metric, not in the trace | ❌ **not wired** — could be added as span metadata |

### 2d. Deliberately out of scope

| Surface | Why |
|---|---|
| **RAG triad** | Argus has no retrieval layer; PostGIS queries are tool calls, not retrieval |
| **Datasets** | TrustModel supports BigQuery only; we are Aurora/PostGIS |
| **Shadow AI discovery** | 50 credits ($5,000, first free) and it scans GitHub + **GCP**; we are AWS. Low signal |
| **AgentCert** | $1,000/cert. Certifies an agent identity — worth doing *after* a score we trust, not before |
| **SKU 3 runtime enforcement** | Q3 2026; would add a synchronous external call before every tool invocation |

---

## 3. Coverage summary

Read this as **"what my exporter can hand TrustModel today"**, not as a statement about what
Argus runs. Argus runs everything in the left column.

| Category | Deployed in Argus | In the trace today | Needs exporter work | Not applicable |
|---|---|---|---|---|
| Agents | 4 (Watch, Investigator, Tasking, Orchestrator) | Orchestrator, with Investigator ×2 and Tasking as nodes | Watch (own trace); per-branch Investigator + Tasking as *separately scored* agents | — |
| MCP servers | 4 | all 4 scanned | — | — |
| Tools | 24 | all 24 scanned; called tools appear per node | — | — |
| Prompts | 4 | all 4 hashed + per-node version | adversarial probing (Red Team) | — |
| Responses | every VOI report | evidence + gaps + priority + confidence | — | — |
| Memory | AgentCore Memory, live | `prior_context_chars` (that it was used) | recalled *text* — not persisted anywhere today | — |
| Guardrail | Bedrock, live on both frameworks | — | `InvocationsIntervened` as span metadata | — |
| RAG / datasets / shadow AI | — | — | — | all three |

**Plain version.** One trace exists, and it covers the Orchestrator run end to end: both
Investigator branches, the Tasking node, the report, all 24 tools, all 4 prompt hashes, the
officer's decision. The four MCP servers are scanned separately and are clean.

The honest shortfall is **two things**:
1. **Watch has no trace yet** — its sweep is a separate job with no manifest (§2a).
2. **Investigator and Tasking are nodes inside the Orchestrator trace, not separately scored
   agents.** They get evaluated as part of the whole, so you get one TrustScore for the
   pipeline rather than four per-agent scores. Whether that matters depends on whether you
   want per-agent governance evidence or one end-to-end score.

---

## 4. Decisions I need before going further

1. **One evaluation or four?** One Orchestrator trace ≈ 1 credit ($100) and is ready now.
   Adding Watch, Tasking and per-branch Investigator is ~4 credits ($400) of your 5 free —
   and Watch needs a second exporter built first (half a day).
   *My recommendation:* run the Orchestrator first, read the real report, confirm the actual
   dimension list, **then** decide whether the other three are worth the credits.

2. **Red Team (prompt scanning)?** The only way to score Prompt Defense properly is 500
   adversarial probes against a live endpoint. That means pointing an attacker at production
   Argus behind Cognito. I would not do that without your explicit go-ahead, a scoped window,
   and ideally a non-production target.

3. **Guardrail signal in the trace?** I can add `InvocationsIntervened` by policy type as span
   metadata. It is read-only (a CloudWatch query) but it is a real addition to the exporter.

Nothing in 1–3 changes the deployment.
