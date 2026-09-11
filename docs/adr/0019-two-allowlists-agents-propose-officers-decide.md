# ADR-0019: Two allowlists, because agents propose and officers decide

Date: 2026-09-09. Status: accepted. Amends ADR-0018.

## Context

The product makes one guarantee above the others: agents propose, humans decide. It is
stated in the README, drawn in the architecture figure, and it is the reason the tasking
flow writes only `proposed` rows. It was not enforced.

ADR-0018 gave the API a single allowlist, `TOOL_ALLOWED_ROLES`, holding both the agent
roles and `argus-operator`. That list answers "may this caller reach the API at all". It
was also, by omission, the answer to "may this caller approve a collection request" and
"may this caller accept a report". The orchestrator role could call
`POST /tasking/{id}/approve` and `POST /investigations/{id}/review` and the API would
allow it: the human in the loop was a convention of the user interface, not a control.

Nothing in the deployment had exercised that path, so nothing had gone wrong. It was a
one-line change away from going wrong, and an audit trail showing an agent role
approving its own proposal is the specific failure this system exists to prevent.

The gap audit lists it as the first of the ten findings that matter most.

## Decision

Three environment variables in place of one, read in `callerauth.py` (both identical
copies) and set by `platform_stack.py`:

- `TOOL_ALLOWED_ROLES`: every role admitted at all. Unchanged in meaning, and still the
  only list the MCP servers use, since they have no officer routes.
- `AGENT_ALLOWED_ROLES`: the roles permitted on agent-only routes (`POST /alerts`, the
  investigation completion, failure and progress callbacks). The agent roles alone.
- `OFFICER_ALLOWED_ROLES`: the roles permitted to take an officer action, meaning review,
  approval and sweep. `argus-operator` alone.

`services/api/main.py` passes `agent_route=is_agent_route` to the middleware, which picks
the list by route rather than applying one list everywhere. `OFFICER_ALLOWED_ROLES` is
fail-closed: unset means no IAM role may take an officer action, and a human signed in
through the balancer is unaffected because that path is checked by `officerauth.py`
before the caller middleware sees the request.

`agent_roles()` falls back to the whole allowlist when `AGENT_ALLOWED_ROLES` is unset, so
the MCP servers keep their single-list behaviour with no configuration change.

## Consequences

An agent role that calls an officer route is refused with the same message an unknown
role gets. `make eval-aws` and any other script that reviews or approves must assume
`argus-operator`; assuming an agent role is no longer sufficient, which is the point.
The route-to-role matrix is pinned in `tests/test_officerauth.py`, so adding an officer
route without adding it to the matrix fails the build rather than shipping open.

The same audit found that personal data was being decrypted into evidence snapshots and
served from `GET /evidence/{id}`. That is fixed alongside this: the API never calls
`pgp_sym_decrypt`, the registry snapshot records only whether a beneficial owner is on
file, and the evidence endpoint names its columns and redacts personal keys. The registry
MCP tool remains the only reader of an owner's name.
