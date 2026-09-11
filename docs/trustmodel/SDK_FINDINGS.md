# Did we implement TrustModel right? — checked against the real SDK

You asked a fair question, so I installed `trustmodel==3.6.0` from PyPI and read its actual
API instead of trusting the wiki. **Verdict: the trace design is sound, but my integration was
built against documentation that is wrong, and it is missing the two features that produce the
governance reports you actually want.**

## What the published wiki gets wrong

| Wiki says | Reality in 3.6.0 |
|---|---|
| `from trustmodel import Client` | ❌ No `Client`. Only **`TrustModelClient`** |
| `client.agentic.evaluate(file_path, goal, agent_framework, agent_model, goal_achieved)` | Close, but **`name` is a required argument** and was missing from every documented example |
| `client.evaluations.create(...)` for agents | That surface exists but is for **model** evals, not agent traces |
| "Python SDK (v2.0.0)" | PyPI latest is **3.6.0** |
| `pip install "trustmodel[telemetry]"` then `auto_init` | `auto_init` lives in `trustmodel.telemetry.auto_init`, not top level |

So the defensive probe I wrote in `evaluate.py` was the right call — but it would still have
failed, because it never passed `name`.

## What I missed entirely

### 1. `frameworks=` — this is the governance report

```python
client.agentic.evaluate(..., frameworks=["owasp-asi", "nist-ai-rmf"], control_ids=[...])
```

Compliance frameworks are a **parameter of the evaluation**, evaluated against named controls.
I treated compliance as something to write up by hand in a crosswalk document. It is a first
class feature, and the two slugs shipped in the package are `owasp-asi` and `nist-ai-rmf`.
`client.frameworks.list()` / `list_domains()` enumerate what the account can use.

Without this the evaluation returns a TrustScore with no control mapping — far less useful for
audit than what the product can actually produce.

### 2. `mcp_scanner` — my local scan can become part of the record

```python
client.mcp_scanner.create_scan(
    server_name=...,
    status="ok" | "warning" | "blocked",
    total_tools=...,
    blocked_tools=...,
    worst_severity=...,
    findings=[McpFinding(...)],
    scanned_at=...,
)
```

`McpFinding{tool_name, risk_score, safe, threats[]}` and
`McpThreat{type, severity, description, evidence}` map almost exactly onto what
`mcp_scan.py` already produces. My scanner output can be **uploaded** so the four Argus MCP
servers get scan reports in the TrustModel console, rather than staying a local JSON file.

Severity vocabulary is `none|low|medium|high|critical`; status is `ok|warning|blocked`. Mine
uses `info|low|medium|high` — **close but not identical**, and `critical` is missing.

### 3. `agentcert.certify` / `issue` / `verify`

Three methods, not the one the wiki shows. Still out of scope at $1,000.

## What my implementation gets right

- **The trace file.** `evaluate()` only checks the file is well-formed JSON or JSONL
  (`_validate_file_content`) — there is **no client-side schema**. The server interprets the
  shape. So the canonical-span design is fine, and my `validate.py` is doing strictly more
  checking than the SDK does.
- **Reading from stored data, not live endpoints.** Nothing in the SDK needs to reach an agent.
- **Hashing by default.** Nothing forces raw content.
- The node-name bug found against production (`c18bb10`) would have made the trajectory empty
  whatever the SDK expected.

## Required changes before spending a credit

| # | Change | Why |
|---|---|---|
| 1 | Pass `name=` to `evaluate()` | Required argument; the call fails without it |
| 2 | Pass `frameworks=["owasp-asi","nist-ai-rmf"]` | This is what produces the compliance report |
| 3 | Drop the `Client` fallback, use `TrustModelClient` | `Client` does not exist |
| 4 | Add `client.frameworks.list()` to the probe | Confirm which slugs this account has |
| 5 | Align severities to `none/low/medium/high/critical` | Ours emits `info`, which is not in their vocabulary |
| 6 | Add `mcp_scanner.create_scan()` upload | Turns a local scan into a governance artefact |
| 7 | Check `client.credits.get_balance()` before submitting | Fail before spending, not during |
| 8 | Use `client.agentic.get_pricing()` | Confirm the real cost instead of quoting the wiki's $100 |

Items 1–4 and 7–8 are small. Item 5 is a vocabulary fix with a test. Item 6 is new but maps
onto data `mcp_scan.py` already produces.

---

# What will actually be tested, and what reports you get

## Test 1 — Agent trajectory evaluation (`agentic.evaluate`)

**Input:** one investigation's `trace.json` — spans per graph node, each with the Nova model,
tier, attempt count and prompt hash; tool calls cited as `server.tool`; the officer's decision
from the append-only audit trail; the guardrail id and version.

**Scored on:** the trust dimensions (Prompt Defense is the 11th, agents only), plus the
compliance controls named by `frameworks=`.

**You get:** a TrustScore 0–100, an `evaluation_run_id`, a console report URL, and a
control-by-control compliance mapping for OWASP ASI and NIST AI RMF.

## Test 2 — MCP tool-surface scan (`mcp_scanner.create_scan`)

**Input:** all 24 tools across the four servers, checked for prompt injection in descriptions,
over-permissive parameters, and unexpected state-changing tools.

**You get:** four scan reports (one per server) in the console, each with a status
(`ok`/`warning`/`blocked`), worst severity, and per-tool findings with risk scores. Currently
all clean, so the expected result is `ok` with zero findings — which is itself the evidence.

## Test 3 — Local pre-flight (free, no credits)

Structural validation and the offline scan, both already working. Catches an empty trajectory
or a malformed trace **before** any charge — which is exactly what caught the node-name bug.

## Not tested, and why

| | Why not |
|---|---|
| Watch agent | `alerts` has no manifest column; needs a second exporter |
| Red Team / Prompt Defense probing | 500 adversarial probes against live production |
| Direct Nova model scan | No public score exists; would need an endpoint breaching ADR-0007 |
| AgentCert | $1,000 |
| RAG / datasets / shadow AI | Not applicable |

## Cost

`agentic.get_pricing()` will give the real number. The wiki says 1 credit ($100) per agent
evaluation and 5 free credits on signup; the MCP scan upload appears to be a plain POST, so
likely free, but I will confirm balance before and after rather than assume.
