-- ADR-0004 Ticket 14: rules submit unified observations; legacy history is read-only.
BEGIN;

ALTER TABLE t_rule_entity_instance_refs
    DROP CONSTRAINT IF EXISTS t_rule_entity_instance_refs_reference_kind_check;
ALTER TABLE t_rule_entity_instance_refs
    ADD CONSTRAINT t_rule_entity_instance_refs_reference_kind_check
    CHECK (reference_kind IN ('source', 'input', 'control', 'alarm'));

CREATE OR REPLACE FUNCTION reject_unsafe_rule_control_actions()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actions JSONB;
    has_entity_input BOOLEAN;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.jdm_content IS NOT DISTINCT FROM OLD.jdm_content THEN
        RETURN NEW;
    END IF;
    actions := COALESCE(NEW.jdm_content->'actions', '[]'::jsonb)
        || COALESCE(NEW.jdm_content->'_config'->'actions', '[]'::jsonb);
    IF jsonb_typeof(actions) <> 'array' THEN
        RAISE EXCEPTION 'rule actions must be an array';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'neuron_write'
    ) THEN
        RAISE EXCEPTION 'legacy neuron_write rule actions are read-only';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'control'
          AND (
              NOT action ? 'entity_instance_id' OR NOT action ? 'value' OR NOT action ? 'id'
              OR jsonb_typeof(action->'id') <> 'string' OR btrim(action->>'id') = ''
              OR action ?| ARRAY['node', 'group', 'tag', 'topic', 'payload', 'command',
                                  'entity_id', 'entity', 'entity_name', 'cooldown']
          )
    ) THEN
        RAISE EXCEPTION 'rule control actions must use entity_instance_id and value only';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'alarm'
          AND (
              NOT action ? 'entity_instance_id' OR NOT action ? 'value'
              OR NOT action ? 'id' OR NOT action ? 'alarm_definition'
              OR jsonb_typeof(action->'id') <> 'string' OR btrim(action->>'id') = ''
              OR jsonb_typeof(action->'alarm_definition') <> 'string'
              OR btrim(action->>'alarm_definition') = ''
              OR action ?| ARRAY['level', 'message', 'node', 'node_id', 'tag', 'tag_id',
                                  'topic', 'payload', 'command', 'entity_id', 'entity', 'entity_name']
          )
    ) THEN
        RAISE EXCEPTION 'rule alarm actions must reference an installed alarm definition and entity instance';
    END IF;
    has_entity_input := EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(NEW.jdm_content->'_config'->'sourceEntityInstanceIds') = 'array'
                THEN NEW.jdm_content->'_config'->'sourceEntityInstanceIds'
                ELSE '[]'::jsonb END
        ) AS source_id
        WHERE btrim(source_id) <> ''
    ) OR EXISTS (
        SELECT 1 FROM jsonb_each(
            CASE WHEN jsonb_typeof(NEW.jdm_content->'_config'->'inputMappings') = 'object'
                THEN NEW.jdm_content->'_config'->'inputMappings'
                ELSE '{}'::jsonb END
        ) AS mapping(field, source_id)
        WHERE jsonb_typeof(source_id) = 'string' AND btrim(source_id #>> '{}') <> ''
    );
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' IN ('control', 'alarm')
    ) AND (NEW.jdm_content->'_config' ? 'sourceNodeIds' OR NOT has_entity_input) THEN
        RAISE EXCEPTION 'rule control and alarm actions require entity instance inputs only';
    END IF;
    IF EXISTS (
        SELECT action->>'id' AS action_id
        FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' IN ('control', 'alarm')
        GROUP BY action_id HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'rule action identifiers must be unique';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION reject_legacy_alarm_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 't_alarms is immutable legacy history; use t_alarm_events';
END;
$$;

-- Preserve historical evidence when a legacy node, tag, entity or rule is
-- retired.  These foreign keys are intentionally removed rather than changed
-- to SET NULL: a referential SET NULL is itself an UPDATE and would violate
-- the immutable-history contract enforced below.  Legacy UUIDs remain opaque
-- historical references after their live object is retired.
ALTER TABLE t_alarms DROP CONSTRAINT IF EXISTS t_alarms_rule_id_fkey;
ALTER TABLE t_alarms DROP CONSTRAINT IF EXISTS t_alarms_node_id_fkey;
ALTER TABLE t_alarms DROP CONSTRAINT IF EXISTS t_alarms_tag_id_fkey;
ALTER TABLE t_alarms DROP CONSTRAINT IF EXISTS t_alarms_entity_id_fkey;

DROP TRIGGER IF EXISTS trg_legacy_alarms_read_only ON t_alarms;
CREATE TRIGGER trg_legacy_alarms_read_only
    BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON t_alarms
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_mutation();

-- An application role receives SELECT-only access in
-- 999_grant_application_role.sh.  Public must never regain a mutation grant;
-- startup also rejects an owner or writable application connection.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON t_alarms FROM PUBLIC;

COMMENT ON TABLE t_alarms IS
    'Read-only legacy history. ADR-0004 events are stored in t_alarm_events.';

COMMIT;
