-- Migration 025: rule inputs use stable entity-instance references.

BEGIN;

CREATE TABLE IF NOT EXISTS t_rule_entity_instance_refs (
    rule_id UUID NOT NULL REFERENCES t_rules(id) ON DELETE CASCADE,
    reference_kind TEXT NOT NULL CHECK (reference_kind IN ('source', 'input')),
    reference_key TEXT NOT NULL,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, reference_kind, reference_key)
);

CREATE INDEX IF NOT EXISTS idx_rule_entity_instance_refs_instance
ON t_rule_entity_instance_refs(entity_instance_id);

CREATE TABLE IF NOT EXISTS t_entity_failover_policies (
    entity_instance_id UUID PRIMARY KEY REFERENCES t_entity_instances(id),
    primary_tag_id UUID NOT NULL REFERENCES t_tags(id),
    standby_tag_id UUID NOT NULL REFERENCES t_tags(id),
    active_source_role TEXT NOT NULL DEFAULT 'primary'
      CHECK (active_source_role IN ('primary', 'standby')),
    switch_count BIGINT NOT NULL DEFAULT 0 CHECK (switch_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (primary_tag_id <> standby_tag_id)
);

CREATE TABLE IF NOT EXISTS t_entity_source_reservations (
    tag_id UUID PRIMARY KEY REFERENCES t_tags(id),
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    source_role TEXT NOT NULL CHECK (source_role IN ('primary', 'standby')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_instance_id, source_role)
);

INSERT INTO t_entity_source_reservations
  (tag_id, entity_instance_id, source_role)
SELECT binding.tag_id, binding.entity_instance_id, 'primary'
FROM t_entity_instance_bindings binding
WHERE binding.active = TRUE
ON CONFLICT (tag_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS t_entity_failover_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    from_role TEXT NOT NULL CHECK (from_role IN ('primary', 'standby')),
    to_role TEXT NOT NULL CHECK (to_role IN ('primary', 'standby')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_role <> to_role)
);

CREATE OR REPLACE FUNCTION reject_entity_failover_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'entity failover audit is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_entity_failover_audit_append_only
ON t_entity_failover_audit;
CREATE TRIGGER trg_entity_failover_audit_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON t_entity_failover_audit
FOR EACH STATEMENT EXECUTE FUNCTION reject_entity_failover_audit_mutation();

ALTER TABLE t_rules DROP CONSTRAINT IF EXISTS chk_rules_no_legacy_entity_refs;

CREATE OR REPLACE FUNCTION reject_new_legacy_rule_entity_refs()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE(NEW.jdm_content->'_config', '{}'::jsonb) ? 'sourceEntityIds'
       AND (
         TG_OP = 'INSERT'
         OR NEW.jdm_content IS DISTINCT FROM OLD.jdm_content
       ) THEN
        RAISE EXCEPTION 'legacy rule entity references are read-only';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_rules_legacy_entity_refs_read_only ON t_rules;
CREATE TRIGGER trg_rules_legacy_entity_refs_read_only
BEFORE INSERT OR UPDATE ON t_rules
FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_rule_entity_refs();

COMMIT;
