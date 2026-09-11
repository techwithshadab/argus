# TrustModel integration plan — one end-to-end audited iteration

**Branch:** `trustmodel` · **Status:** plan only, no code written yet, no key held yet.

The goal is narrow and testable: take **one** Argus investigation, from the officer pressing
Investigate to the report and tasking proposal, and produce a **TrustModel report** that an
auditor or governance reviewer can read alongside our own audit trail.

Everything below is drawn from the TrustModel wiki (read 2026-09-11). Where the wiki does not
publish a detail, this plan says so rather than inventing it.

---

## 1. What TrustModel is, in our terms

Four SKUs sharing one TrustScore data model. Credits are one currency: **1 credit = $100**,
new accounts get **5 free credits ($500)**.

| SKU | Product | Status | What it would do for Argus |
|---|---|---|---|
| 1 | **AI Assurance** | Live | Score one investigation's trace → 0–100 TrustScore + regulator-ready report |
| 2 | **Continuous Monitoring** | Live | Ingest our existing OTel spans; trust-drift alerts |
| 3 | **Agent Governance (AGP)** | Q3 2026 | Runtime allow/deny/redact; **MCP server scanning**; shadow-AI discovery |
| 4 | **AgentCert** | New | Signed certificate binding agent identity to an eval run ($1,000/cert) |

### Trust dimensions

The wiki never prints one clean list of all ten. Names confirmed across the OWASP page and the
product pages: **Prompt Defense** (the 11th, agents only), **Privacy**, **Safety**,
**Robustness**, **Accountability**, **Accuracy**, **Faithfulness/Groundedness**, **Reliability**,
plus **bias/fairness** (`trust_score.fairness` appears in the alerting example) and **security**.
Treat the exact set as *to be confirmed from the console on first run* — do not quote a
dimension list in any governance doc until we have seen it in a real report.

---

## 2. What we can actually scan

You asked specifically what is scannable. Mapping TrustModel's surface onto Argus:

| Argus asset | Scannable? | TrustModel surface | Notes |
|---|---|---|---|
| **4 agents** (Watch, Investigator, Tasking, Orchestrator) | ✅ Yes | SKU 1 agent eval | Needs a trace per agent run; adds Prompt Defense dimension |
| **4 MCP servers** (ais, geo, imagery, registry — 24 tools) | ✅ Yes | `trustmodel_mcp_scan_server` (SKU 3) | Scans tool descriptions for **tool poisoning, prompt injection, over-permissive grants** |
| **Prompts** (`agents/shared/prompts/*.md`) | ✅ Indirectly | Prompt Defense + LLM07 probes | Red Team probes attempt system-prompt extraction |
| **Responses** (VOI reports) | ✅ Yes | Accuracy + Groundedness | Scores evidence-based vs fabricated — maps to our `validate_report` |
| **Memory** (AgentCore Memory recall) | ⚠️ Partial | Only as trace content | No dedicated memory scanner documented; it appears as span content |
| **Tool calls** (24 tools over PostGIS) | ✅ Yes | Agent eval trajectory | Tool calls are first-class in the canonical schema |
| **RAG** | ➖ N/A | — | Argus has no retrieval layer; skip the RAG triad |
| **Datasets** | ➖ Skip | BigQuery only | We are Aurora/PostGIS; not supported |
| **Shadow AI discovery** | ⚠️ Costly | 50 credits ($5,000), first scan free | Scans GitHub org + GCP; we are AWS. Low value here |

**Recommendation:** scan **agents + MCP servers + responses**. Skip RAG, datasets and shadow
discovery for this iteration.

---

## 3. Why Argus is unusually well-suited to this

We already persist, per investigation, almost exactly what an agent evaluation wants:

- `investigations.trace_id` — the OTel trace id (`data/sql/001_schema.sql:75`)
- `investigations.manifest` JSONB — per-node **model, provider, tier, attempts, prompt hash**,
  MCP server versions, code revision (`agents/shared/provenance.py:43`)
- `investigations.report` JSONB — the VOI report with dotted `server.tool` evidence citations
- `audit_events` — append-only by trigger, with `trace_id` and `manifest_ref`

So the exporter can be built **entirely from the database**, with **no changes to agent code**
and no redeploy. That matters: a prompt edit would cut a new immutable Bedrock Prompt version
(ADR-0012) and require `evals/node_evals.py --gate` to pass. This plan avoids touching prompts.

---

## 4. The canonical trace schema

The only schema the wiki publishes (from the transformers page):

```json
{
  "trace_id": "...",
  "span_id": "...",
  "ts": "ISO-8601",
  "model": "gpt-4-turbo",
  "input_hash": "sha256:...",
  "output_hash": "sha256:...",
  "tokens": {"input": 87, "output": 142},
  "latency_ms": 1215,
  "tool_calls": [...],
  "metadata": {...}
}
```

The exact `trace.json` envelope for `agentic.evaluate(file_path=...)` is **not published**.
Two mitigations, in order:
1. Emit the canonical shape above as a list of spans and let their transformer normalise it.
2. Register a **custom transformer** (`BaseTelemetryTransformer`, "~50 lines") named
   `argus-otel` if their loader rejects our envelope.

⚠️ Note `input_hash`/`output_hash` are **hashes by default** — raw content only flows when
`raw_traces=True`. Given `registry.beneficial_owner` is encrypted at rest and the API stays
pseudonymous, **we default to hashes** and turn on raw traces only for a deliberate,
non-personal subset. This is a privacy decision to make explicitly, not by default.

---

## 5. The end-to-end iteration

Seven steps. Steps 1–3 need **no API key** and can be built and tested before you hand one over
(`pip install trustmodel-local` / `npx @trustmodel/mcp-server` are free and keyless).

| # | Step | Needs key? | Cost | Output |
|---|---|---|---|---|
| 1 | **Export** one investigation → `trace.json` | No | free | `artifacts/<id>/trace.json` |
| 2 | **Validate** shape locally (`trustmodel-local`) | No | free | pass/fail, no upload |
| 3 | **Scan MCP servers** — 4 servers, 24 tools | No (local) | free | tool-poisoning / injection findings |
| 4 | **Evaluate** the agent trace (SKU 1) | **Yes** | ~1 credit ($100) | TrustScore + `evaluation_run_id` |
| 5 | **Pull the report** (JSON + console URL) | Yes | included | `artifacts/<id>/trustmodel_report.json` |
| 6 | **Cross-walk** to our own audit trail | No | free | `AUDIT_CROSSWALK.md` |
| 7 | *(optional)* **AgentCert** | Yes | $1,000 | Signed cert — **skip unless you want it** |

**Budget:** one full iteration costs **1 credit ($100)** of the 5 free credits. Steps 1–3 and 6
cost nothing. I will not run step 7 without you asking.

---

## 6. What gets built

```
services/trustmodel/
  __init__.py
  export_trace.py      # DB → canonical trace.json (pure; unit-testable)
  transformer.py       # ArgusOtelTransformer(BaseTelemetryTransformer)
  evaluate.py          # thin client wrapper; reads TRUSTMODEL_API_KEY
  mcp_scan.py          # scan the 4 MCP servers' tool manifests
  report.py            # TrustModel JSON → governance markdown + crosswalk
tests/test_trustmodel_export.py   # pure, no network, no DB (CI rule)
docs/trustmodel/PLAN.md           # this file
docs/trustmodel/REPORT.md         # generated, per run
```

Constraints this respects, from `CLAUDE.md`:
- Unit tests import no boto3/httpx/psycopg; the trace-shaping logic is **pure** and tested,
  the DB read and the network call are thin and kept out of unit tests.
- New dep pinned `==` in its own `requirements.txt`; ruff line length 88; rules E,F,I,B,UP.
- No agent/prompt changes → **no Bedrock prompt version, no eval gate, no redeploy**.
- Key comes from env/Secrets Manager, never CDK context (same rule as `AISSTREAM_API_KEY`).

---

## 7. The governance report

Step 6 is what actually makes this worth doing. TrustModel gives an external, independent
score; Argus gives an internal, tamper-evident trail. The crosswalk puts them side by side:

| Evidence | Argus (internal) | TrustModel (independent) |
|---|---|---|
| What ran | `manifest`: model, tier, prompt hash, attempts | Trace spans + model id |
| Who acted | `audit_events` actor/actor_kind, append-only by trigger | Merkle-style tamper-evident log (SKU 3) |
| Was the report grounded | `validate_report` + dotted `server.tool` citations | Accuracy / Groundedness dims (LLM09) |
| Prompt-injection resistance | Guardrail `PROMPT_ATTACK` LOW; `untrusted()` wrapping | Prompt Defense dim (LLM01/LLM07) |
| Least privilege | Cedar ENFORCE on the tools gateway | AGP least-privilege (LLM06) |
| Human-in-the-loop | tasking stays `proposed`; officer-only approve | Accountability dim |

Framework mapping to claim: **NIST AI RMF**, **ISO 42001**, **EU AI Act**, **OWASP LLM Top 10**
(TrustModel publishes a full LLM01–LLM10 coverage table; all ten covered).

---

## 8. Open questions I could not settle from the wiki

1. **Exact `trace.json` envelope** for `agentic.evaluate` — not published. Plan: try canonical
   shape, fall back to a custom transformer.
2. **The definitive 10-dimension list** — never printed in full. Confirm from the first report.
3. **`client.agentic.*` vs `client.evaluations.*`** — the quickstart and the API reference use
   *different* client surfaces (`client.agentic.evaluate` vs `client.evaluations.create`), and
   the API reference shows `TrustModelClient` while the quickstart shows `Client`. Likely a
   versioning drift in their docs. Resolve against the installed package's actual API.
4. **Local package parity** — whether `trustmodel-local` can validate an agent trace offline or
   only scores models. Determines how much of step 2 is real.

I will resolve 1, 3 and 4 by reading the installed package rather than guessing.

---

## 9. What I need from you

1. The **TrustModel API key** (`tm-...`) — I will read it from `.env` / environment only, never
   commit it, never put it in CDK context.
2. A decision on **`raw_traces`**: hashes only (default, safest) or raw content for a
   non-personal subset. Argus handles encrypted beneficial-owner data, so I default to hashes.
3. Confirmation to spend **1 credit ($100)** on the single agent evaluation in step 4.
4. Whether you want **AgentCert** ($1,000) — my recommendation is **no** for this iteration.

Sources: [TrustModel Wiki](https://trustmodel.ai/wiki),
[Install](https://trustmodel.ai/wiki/install),
[Quickstart](https://trustmodel.ai/wiki/quickstart),
[Assurance](https://trustmodel.ai/wiki/assurance),
[Telemetry](https://trustmodel.ai/wiki/telemetry),
[Transformers](https://trustmodel.ai/wiki/telemetry/transformers),
[AGP](https://trustmodel.ai/wiki/agp),
[OWASP coverage](https://trustmodel.ai/wiki/owasp-coverage),
[Pricing](https://trustmodel.ai/wiki/pricing),
[Developers](https://trustmodel.ai/developers)
