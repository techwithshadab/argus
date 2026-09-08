-- Phase 4: provenance manifest per investigation (ADR-0006). Idempotent.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS manifest JSONB;
