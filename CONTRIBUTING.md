# Contributing To Argus

Thank you for looking. This is a demonstration system for maritime domain awareness on
Amazon Bedrock AgentCore, and it is held to production standards because the point of it
is to show what production looks like.

## Before You Start

Read `CLAUDE.md`. It is the working agreement for this repository and it records the
things that have already gone wrong, so you do not have to rediscover them. `CONTEXT.md`
has the vocabulary (alert, sweep, investigation, report, action, review state versus
approval); using the wrong word for one of those makes a change hard to review.

Check `docs/adr/` before reversing an architectural choice. If you disagree with one,
write a new decision record that supersedes it rather than quietly changing the code.

## The Gates

Run all of these before opening a pull request. Continuous integration runs the same set.

```
uvx ruff@0.16.5 check .
uvx ruff@0.16.5 format --check .
pytest -q
docker compose config -q
make synth          # when you touch infra/
```

Ruff is not installed globally; the pinned version above matches continuous integration.
Formatting is at line length 88. Do not add `# noqa` for a rule that is not selected.

## Testing Rules

Unit tests in `tests/` must run with only `pytest`, `pyyaml` and `pydantic` installed,
with no Docker, no database and no AWS. That is exactly what continuous integration
installs. Do not import `strands`, `langgraph`, `httpx`, `psycopg` or `boto3` from a unit
test; if you need to test logic that lives next to one of those imports, move the logic
into a pure module first. Several helpers already live in `agents/shared/`,
`evals/scoring.py` and `services/ais-replay/areas.py` for exactly this reason.

Tests that need a running stack carry `@pytest.mark.integration` and must skip cleanly
when it is down.

Run pytest from the repository root. Tests rely on relative paths.

## What A Good Change Looks Like

- **It fixes one thing.** A pull request that fixes a bug and reorganises a module is two
  pull requests.
- **It carries a test that fails without it.** Especially for a bug: the test is the
  description of what was wrong.
- **Its comments say why, not what.** The code says what. If a line looks strange, the
  comment should explain the failure that made it necessary.
- **It updates the documentation it invalidates.** Figures carry counts that a test
  checks; the runbook lists every alarm; the architecture page names code paths.
- **It does not widen an interface without need.** New agent-only API routes go in
  `_AGENT_ROUTES` or they are open by default.

## Things That Will Be Sent Back

- A model choosing something the code should decide. Detection, sequencing, vessel
  identifiers and time windows are not model decisions, for reasons recorded in the
  decision records.
- Personal data reaching the API, a report or the user interface. One tool decrypts owner
  names. Everything else stays pseudonymous.
- Weakening the human gate. Agents create proposals; officers approve them.
- An unpinned dependency, or an AWS resource created outside CDK.
- A number in prose that no test checks.

## Reporting A Vulnerability

Please do not open a public issue. See `SECURITY.md`.
