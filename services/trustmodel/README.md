# TrustModel integration

Produces an independent, audit-ready assessment of one Argus investigation, to sit alongside
our own append-only audit trail. Plan and wiki research: `docs/trustmodel/PLAN.md`.

## Design

Everything reads from the **database and `mcp-servers/tools.json`**. No agent code changes, no
prompt edits, no redeploy — a prompt change would cut a new immutable Bedrock Prompt version
(ADR-0012) and require `evals/node_evals.py --gate` against a running stack.

The shaping logic is pure so the unit tests stay dependency-free (CLAUDE.md): `trace_shape`,
`mcp_scan` and `validate` import only the standard library. The DB read (`export_trace`) and
the network call (`evaluate`) are thin and kept out of unit tests.

| Module | Does | Pure? |
|---|---|---|
| `trace_shape.py` | investigation + audit rows → canonical spans | yes |
| `mcp_scan.py` | scans 24 tools for injection / over-permission / unexpected writes | yes |
| `validate.py` | structural checks before any credit is spent | yes |
| `transformer.py` | `BaseTelemetryTransformer` fallback if their loader rejects our envelope | yes |
| `export_trace.py` | reads the DB, writes `trace.json` | no (psycopg) |
| `evaluate.py` | step 4, the paid submission | no (SDK) |
| `run_local.py` | runs steps 1–3 together | — |

## Running

Steps 1–3 are free and need no API key.

```bash
# export the latest completed investigation, then validate and scan
python -m services.trustmodel.run_local --latest

# or re-run the checks against an existing file, with no database at all
python -m services.trustmodel.run_local --trace artifacts/trustmodel/<id>/trace.json
```

Step 4 spends about 1 credit ($100) and is guarded twice — it refuses an invalid trace, and
does nothing without `--confirm`:

```bash
export TRUSTMODEL_API_KEY=tm-...
python -m services.trustmodel.evaluate --probe                 # free: inspect the SDK
python -m services.trustmodel.evaluate --trace <path>          # dry run
python -m services.trustmodel.evaluate --trace <path> --confirm  # spends
```

## Privacy

Content is **hashed by default** (`sha256:<hex>`); only hashes and metadata leave the machine.
`registry.beneficial_owner` and person names exist solely encrypted (`pgp_sym_encrypt`, three
`datakey.py` copies) and the API stays pseudonymous, so raw upload is a deliberate decision:
`--raw` embeds report text and should only be used on a knowingly non-personal subset.

The API key is read from the environment / `.env` only — never CDK context, the same rule as
`AISSTREAM_API_KEY`.

## What the scan covers

`mcp_scan` runs three checks over all 24 tools in 4 servers, mirroring what TrustModel's Agent
Governance SKU does at registration time (that SKU is Q3 2026, so this runs locally):

- **prompt_injection** — instruction override, role reassignment, chat-role markup or
  credential references in a tool description, which is attacker-controlled text from the
  agent's point of view.
- **excessive_permission** — parameters (`command`, `sql`, `path`, `url`, …) that widen a
  tool's reach beyond a maritime data read.
- **state_change** — write-verb tools other than `imagery.create_tasking_request`, the single
  expected writer, which only ever creates a `proposed` row for human approval.

These are heuristics over tool metadata, not a proof of safety. A clean result means no
injection pattern was found in the text an agent is asked to trust — nothing more.

## Known gaps

- The exact `trace.json` envelope for `agentic.evaluate` is not published. We emit the
  canonical transformer schema under both `spans` and `trace`; `transformer.py` is the
  documented fallback.
- The full 10-dimension list is never printed in the wiki. Confirm it from the first real
  report rather than quoting an invented list in a governance document.
- The wiki's API reference (SDK v2.0.0, `TrustModelClient.evaluations.create`) disagrees with
  its own quickstart (`Client.agentic.evaluate`) and with PyPI (3.6.0). `--probe` resolves it
  against the installed package.
