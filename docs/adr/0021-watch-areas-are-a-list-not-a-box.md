# ADR-0021: The watch is a list of areas, not one box

Date: 2026-09-09. Status: accepted.

## Context

Live mode subscribed AISStream to exactly one bounding box: the scenario's. That made
sense while the scenario was the product, and stopped making sense the moment anyone
asked to watch somewhere else. Changing the watched water meant editing a scenario file
and redeploying, and there was no way to watch two regions at once, which is what a real
watch floor does.

The demonstration also suffered. A single box in the eastern Mediterranean shows one kind
of traffic. The interesting comparisons, a Gulf of Guinea rendezvous against a Malacca
Strait one, were not reachable.

## Decision

`data/areas.yaml` is a catalogue of named regions, each with a label and a bounding box.
`WATCH_AREAS` selects from it: `all`, `scenario` for the scenario box alone, or a comma
separated list of names. The scenario's own area is always first, and a catalogue entry
whose box matches it is skipped rather than subscribed twice.

Every selected box goes into one AISStream subscription. The resolved list is published as
`area.areas` through `scenario_meta`, and the watch floor's "watching" selector filters the
map by it, client side, in both live and replay mode. In replay mode the extra areas have
no data, which is correct: the selector still works, it just shows an empty region.

An unknown name raises at start rather than being ignored, so a typo is noticed at deploy
time instead of quietly watching nothing.

The selection logic is a pure module, `services/ais-replay/areas.py`, and unit-tested.

## Consequences

Watching more water writes more positions and produces more sweep candidates, and both
scale roughly with the area. That is the cost, and it is the operator's to choose. The
ingest task now validates coordinates before insert, which it needed anyway once real
traffic from many regions arrived.

Deploy passes `-c watchAreas`; the value never enters a scenario file.
