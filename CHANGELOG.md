# Changelog

Notable changes to Argus. Dates are when the work landed, not when it deployed.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project
does not carry version numbers yet; each entry is a dated body of work.

## Unreleased

### Security

- Agent roles and the operator role are separate allowlists, enforced per route. An agent
  role can no longer approve a collection request or review its own findings (ADR-0019).
- The API no longer decrypts beneficial owner names. Evidence snapshots record only
  whether an owner is on file, and the evidence endpoint redacts personal keys.
- Recording an evaluation score requires the operator role, and the audit entry names the
  principal that wrote it.
- The operator role's account-wide trust now requires multi-factor authentication.
  Continuous integration assumes it through a GitHub identity provider instead of a
  long-lived access key.
- Every container built here runs as a non-root user. The user interface moved to the
  unprivileged nginx image.

### Added

- Multi-region watching: `WATCH_AREAS` selects named regions from `data/areas.yaml`
  alongside the scenario box, and the user interface filters by them.
- A feed heartbeat metric from the ingest task, with an alarm that treats silence as a
  breach, so a dead feed pages even when the API and collector are down.
- Alarms for sign-in failures, rolled-back deployments and sweeps that raise nothing. The
  evaluation gate alarms now breach when no run has reported.
- A monthly budget with actual and forecast notifications.
- A restore procedure in the runbook, and a database snapshot before every destroy.
- Template assertions in continuous integration, plus dependency auditing, image scanning
  and Dependabot.
- Apache 2.0 licence, contributing guide, model card and this changelog.

### Changed

- Records are retained by default. Deleting the database or the archive is now an explicit
  choice, and `make destroy-keep-data` keeps them while removing the compute.
- The API, user interface and telemetry collector run two tasks and stay up through a
  deploy.
- Alerts are deduplicated by a unique index on the anomaly window, checked against every
  non-dismissed alert rather than only open ones, and unreviewed drafts expire after three
  days with an audit entry.
- The evaluation gate scores only the run under test, and duplicate alerts cost precision.
- Collection requests are centred on the vessel's last known position, resolved in code,
  and rejected if more than 200 nautical miles away.
- A failed line of enquiry caps the report's confidence and priority (ADR-0020).
- Trace indexing samples at a tenth rather than indexing every span.

### Fixed

- The ingest task no longer crashes on out-of-range AIS coordinates, and drops null-island
  reports that were manufacturing false identity conflicts.
- Completing, failing and reviewing are guarded state transitions; a replayed call no
  longer overwrites a reviewed report, and a second review no longer flips the first
  officer's decision.
- Alert evidence cites tools by one canonical name in both gateway and direct modes.
- The watch floor reconnects its event stream after a session expiry or a rollout, instead
  of claiming to retry forever.
- Review shortcuts act on the highlighted alert, not the first alert for that vessel.
- Drawing no longer scans the alert list once per vessel, and open evidence panels survive
  the list refresh.
