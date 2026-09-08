# Diagrams

Every figure the docs embed (four), rendered from a committed source so a diagram can never drift
from something regenerable.

| Figure | Embedded in | Shows |
|---|---|---|
| `architecture-overview.png` | README, ARCHITECTURE.md §End-to-end | The high-level view for readers who do not need the technology (1640×510): five blocks left to right, Sources, then Watch, Investigate and Propose inside the Argus container with a strip of what every step carries, the boundary where people decide, and the watch floor; two labelled flows in and one decision flow back. |
| `investigation-graph.png` | ARCHITECTURE.md §The investigation graph, docs/USE_CASES.md UC-3 | The code-owned graph (1640×512): three triggers into one durable job, the two Investigator branches in parallel joining at a pure merge, Tasking, the report writer with its policy and schema check and one correction loop, persistence, vessel memory feeding the next run, and the watch floor where the draft is reviewed, the proposal approved and every decision audited. |
| `data-model.png` | ARCHITECTURE.md §Data model, docs/TECHNICAL.md | The PostGIS tables as generated table boxes (1880×509) in three concerns: what the feeds said, what Argus concluded (referencing columns highlighted, references drawn as arrows), and the record. |
| `technical-architecture.png` | README, ARCHITECTURE.md §AWS deployment, docs/TECHNICAL.md | The AWS reference-style view with official service icons (1900×840): a full-height column outside AWS for the watch floor (officers over HTTPS, operators with an IAM caller token) and the four external feeds joined by one trunk; a top row of edge (WAF, Cognito sign-in), jobs and scheduling, deployment, and identity, secrets and configuration; the VPC with the public subnet, the application on ECS Fargate, the data subnets under it, the self-hosted monitoring task (collector, Grafana stack) under those, and the agents and MCP servers on the right; AgentCore (control plane) and Bedrock beside them; AWS-side monitoring as two columns (telemetry: AgentCore Observability, CloudWatch, SNS; and Transaction Search, X-Ray traces, operator email). The AIS ingest task shows its writes: events to ElastiCache, positions to Aurora. Rows are aligned across boxes, boxes are sized to their content with a margin between neighbours, node labels are plain text (no chip) and never sit on a box border, and connectors run icon edge to icon edge (or from a label straight down to the next icon) without crossing each other, an icon or a label. |

## How they are built

Each figure is a small HTML page under [`src/`](src/) sharing one design system
([`src/_design.css`](src/_design.css): the palette, column/card/step components, trust-boundary
and plane styles). Rendering is a headless-Chrome screenshot at 2x:

```bash
make diagrams                       # renders every PNG, then checks the counts
sh docs/diagrams/src/render.sh      # render only
python docs/diagrams/src/check_counts.py
```

Conventions every figure follows (the same rules as the technical architecture):

- One dark palette (navy ground, sea-teal wires, red dashed trust boundaries), one icon and one plain-text name per node, boxes sized to their content with a margin between neighbours, rows aligned across boxes.
- Orthogonal connectors from icon edge to icon edge, a visible shaft before every arrowhead, junction dots where a wire branches, labels beside wires and never on a box border; no wire crosses another wire, an icon, a label or a title.
- Every figure is inspected at full resolution (quadrant crops) before its PNG is committed.
- Icons identify what a node is: the official
  [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/) (release 07-31-2026, used under
  AWS's icon terms, unmodified) for AWS services, resources and groups, and each product's own mark for
  everything else (Simple Icons for Grafana, Prometheus, PostgreSQL, OpenTelemetry, Docker, LangGraph,
  LangChain, MCP, Python, FastAPI, nginx, OpenStreetMap; Grafana Labs' Tempo and Loki marks; the Valkey,
  Strands Agents and OpenSanctions marks from their projects). One icon per node, never a badge on
  top of another icon: an AWS icon for an AWS resource, the product's mark for software Argus runs in a
  container. They live under `src/icons/`; refresh them with `sh src/icons/fetch.sh` when a pack is
  updated. The marks belong to their owners.
- Every number in a figure is wrapped as `<span data-count="key">N</span>` and checked against the
  repository by `src/check_counts.py` (run in CI by `tests/test_diagrams.py`), so a figure cannot
  claim a count the code does not back.

To change a figure, edit its `src/*.html`, run `make diagrams`, and commit both the source and
the PNG together.
