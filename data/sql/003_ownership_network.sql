-- Ownership network (ADR-0004): vessels, companies, people, sanction listings and flags as
-- entities, typed relationships as edges, traversed with a recursive CTE. Idempotent.
--
-- Personal data: a person's real name lives only in entities.name_enc (pgp_sym_encrypt with the
-- deployment data key); entities.name holds a pseudonymous label. Companies are not personal data.

CREATE TABLE IF NOT EXISTS entities (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('vessel', 'company', 'person', 'sanction_listing', 'flag')),
  key         TEXT NOT NULL UNIQUE,                 -- kind:normalised identity, e.g. vessel:511666006, company:halcyon marine management fze
  name        TEXT NOT NULL,                        -- display name (pseudonym for persons)
  name_enc    BYTEA,                                -- encrypted real name (persons only)
  attrs       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_kind_idx ON entities (kind);

CREATE TABLE IF NOT EXISTS edges (
  id          BIGSERIAL PRIMARY KEY,
  src         BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst         BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel         TEXT NOT NULL CHECK (rel IN ('owns', 'operates', 'beneficially_owns', 'listed_on',
                                           'rendezvoused_with', 'flagged', 'reflagged_from', 'fleet_of')),
  since       TIMESTAMPTZ,
  until       TIMESTAMPTZ,
  source      TEXT NOT NULL DEFAULT 'registry',     -- registry | ais | opensanctions | analyst
  confidence  REAL NOT NULL DEFAULT 1.0,
  attrs       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS edges_src_idx ON edges (src);
CREATE INDEX IF NOT EXISTS edges_dst_idx ON edges (dst);
CREATE INDEX IF NOT EXISTS edges_rel_idx ON edges (rel);

-- Upsert helpers used by loaders and tools.
CREATE OR REPLACE FUNCTION entity_upsert(p_kind TEXT, p_key TEXT, p_name TEXT, p_attrs JSONB DEFAULT '{}'::jsonb, p_name_enc BYTEA DEFAULT NULL)
RETURNS BIGINT LANGUAGE sql AS $$
  INSERT INTO entities (kind, key, name, name_enc, attrs) VALUES (p_kind, p_key, p_name, p_name_enc, coalesce(p_attrs, '{}'::jsonb))
  ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, name_enc = coalesce(EXCLUDED.name_enc, entities.name_enc),
                                  attrs = entities.attrs || EXCLUDED.attrs
  RETURNING id;
$$;

CREATE OR REPLACE FUNCTION edge_upsert(p_src BIGINT, p_dst BIGINT, p_rel TEXT, p_source TEXT DEFAULT 'registry',
                                       p_since TIMESTAMPTZ DEFAULT NULL, p_until TIMESTAMPTZ DEFAULT NULL,
                                       p_confidence REAL DEFAULT 1.0, p_attrs JSONB DEFAULT '{}'::jsonb)
RETURNS BIGINT LANGUAGE sql AS $$
  INSERT INTO edges (src, dst, rel, source, since, until, confidence, attrs)
  VALUES (p_src, p_dst, p_rel, p_source, p_since, p_until, p_confidence, coalesce(p_attrs, '{}'::jsonb))
  ON CONFLICT (src, dst, rel) DO UPDATE SET source = EXCLUDED.source, since = coalesce(EXCLUDED.since, edges.since),
                                            until = coalesce(EXCLUDED.until, edges.until), confidence = EXCLUDED.confidence,
                                            attrs = edges.attrs || EXCLUDED.attrs
  RETURNING id;
$$;

-- k-hop neighbourhood of an entity, undirected, with the shortest path to every reached node and the
-- sanction listings found. This is the Investigator's Graph RAG context.
CREATE OR REPLACE FUNCTION ownership_network(p_key TEXT, p_depth INT DEFAULT 2)
RETURNS JSONB LANGUAGE sql STABLE AS $$
WITH RECURSIVE start AS (
  SELECT id FROM entities WHERE key = p_key
), walk AS (
  SELECT id, 0 AS depth, ARRAY[id] AS path FROM start
  UNION ALL
  SELECT nxt.id, w.depth + 1, w.path || nxt.id
  FROM walk w
  JOIN LATERAL (
    SELECT CASE WHEN e.src = w.id THEN e.dst ELSE e.src END AS id
    FROM edges e WHERE e.src = w.id OR e.dst = w.id
  ) nxt ON TRUE
  WHERE w.depth < p_depth AND NOT (nxt.id = ANY (w.path))
), reached AS (
  SELECT DISTINCT ON (id) id, depth, path FROM walk ORDER BY id, depth
), nodes AS (
  SELECT n.id, n.kind, n.key, n.name, n.attrs, r.depth, r.path
  FROM reached r JOIN entities n ON n.id = r.id
), sel_edges AS (
  SELECT e.* FROM edges e
  WHERE e.src IN (SELECT id FROM reached) AND e.dst IN (SELECT id FROM reached)
)
SELECT jsonb_build_object(
  'root', p_key,
  'depth', p_depth,
  'found', EXISTS (SELECT 1 FROM start),
  'nodes', (SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'kind', kind, 'key', key, 'name', name, 'attrs', attrs, 'depth', depth)
                                     ORDER BY depth, kind, name), '[]'::jsonb) FROM nodes),
  'edges', (SELECT coalesce(jsonb_agg(jsonb_build_object('src', src, 'dst', dst, 'rel', rel, 'since', since, 'until', until,
                                                         'source', source, 'confidence', confidence, 'attrs', attrs)), '[]'::jsonb)
            FROM sel_edges),
  'listed', (SELECT coalesce(jsonb_agg(jsonb_build_object(
               'key', n.key, 'name', n.name, 'depth', n.depth,
               'path', (SELECT jsonb_agg(e2.name ORDER BY u.ord) FROM unnest(n.path) WITH ORDINALITY AS u(id, ord) JOIN entities e2 ON e2.id = u.id))
               ORDER BY n.depth), '[]'::jsonb)
             FROM nodes n WHERE n.kind = 'sanction_listing')
);
$$;
