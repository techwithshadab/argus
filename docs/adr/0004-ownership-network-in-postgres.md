---
status: accepted
---
# The ownership network is modelled as a graph inside PostGIS, not in a graph database

Dark-fleet analysis is about relationships: shared operators, beneficial owners two hops from a listed entity, vessels that rendezvous with each other. The registry stored these as flat columns and JSON blobs, so only single-vessel lookups were possible. Decision: explicit entity tables (vessel, company, person, sanction listing, flag) and a typed edge table (owns, operates, beneficially_owns, listed_on, rendezvoused_with, reflagged_from), traversed with recursive CTEs and exposed to the Investigator as an `ownership_network(mmsi, depth)` MCP tool whose k-hop subgraph is passed as structured context. That is the Graph RAG step.

## Considered options

- Neptune or Neo4j now: native traversal and community detection, at the cost of a second store, schema and sync pipeline before data volume justifies it.
- Keep JSON blobs: multi-hop questions stay unanswerable.

## As built (phase 2)

`entities` (vessel, company, person, sanction_listing, flag) and `edges` (owns, operates, beneficially_owns, listed_on, flagged, reflagged_from, fleet_of, rendezvoused_with) with `entity_upsert` / `edge_upsert` helpers and an `ownership_network(key, depth)` SQL function that returns nodes, edges and the shortest path to every sanction listing reached. The loader derives the graph from registry rows; rendezvous edges come from the loaded positions using the same proximity rule as the AIS detector (two vessels within 0.5 nm, both under 3 kn, for at least 30 minutes), so they are data-derived, never taken from ground truth. Persons are personal data: their real name is stored encrypted and the node carries a pseudonym; only the registry MCP tool decrypts, the UI endpoint stays pseudonymous.

## Consequences

One database, one backup story, one transaction boundary between positions, alerts and the network. Revisit when traversal depth exceeds what recursive CTEs handle within the Investigator's per-node timeout, or when graph algorithms (community detection) become a product feature. The edge table is designed so an export to a property graph is a projection, not a rewrite.
