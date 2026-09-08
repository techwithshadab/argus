---
status: accepted
---
# Every investigation stores a provenance manifest

A stored VOI Report recorded only a trace id, and traces are retention-limited. Prompts were unversioned files, MCP servers exposed no version, the report schema had no version and the model used was recorded nowhere durable. Decision: each Investigation persists a manifest with the git SHA, per-agent prompt id and content hash, per-node provider, model id and parameters, MCP server name and semver (also exposed on `/health` and stamped on every tool span), report schema version, and references to the evidence snapshots. Prompts stay in git with a version header; the recorded hash is the source of truth, so no external prompt store is required.

## Considered options

- Prompt and model versions only: cheaper, but a changed detector threshold silently changes what a replay would say.
- Trace id only: traces are not evidence.

## As built (phase 4)

The graph builds the manifest (`shared/provenance.py`): `GIT_SHA` baked into the images, SHA-256 prefixes of every prompt file, per-node provider, model id, tier, prompt hash and attempts (specialists expose theirs on `/provenance`), MCP server versions read from each `/health` (the servers carry `VERSION` and stamp it on every tool span as `mcp.server.version`), the schema version, and, added by the API on completion, the ids of the evidence snapshots. Stored in `investigations.manifest` and shown in the report view.

## Consequences

Changing a prompt, a tool or a schema is visible in the next manifest, which is what per-node evals key on for regression attribution. the trace store prompt management can be layered on later by mapping its version ids to the same hashes.
