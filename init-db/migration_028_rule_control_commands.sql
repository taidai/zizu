-- Ticket 10: rules and policies only create auditable control commands.
BEGIN;

ALTER TABLE t_control_commands
    ADD COLUMN IF NOT EXISTS origin_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE t_rule_entity_instance_refs
    DROP CONSTRAINT IF EXISTS t_rule_entity_instance_refs_reference_kind_check;
ALTER TABLE t_rule_entity_instance_refs
    ADD CONSTRAINT t_rule_entity_instance_refs_reference_kind_check
    CHECK (reference_kind IN ('source', 'input', 'control'));

CREATE OR REPLACE FUNCTION reject_unsafe_rule_control_actions()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actions JSONB;
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
        SELECT 1
        FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'neuron_write'
    ) THEN
        RAISE EXCEPTION 'legacy neuron_write rule actions are read-only';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'control'
          AND (
              NOT action ? 'entity_instance_id'
              OR NOT action ? 'value'
              OR NOT action ? 'id'
              OR jsonb_typeof(action->'id') <> 'string'
              OR btrim(action->>'id') = ''
              OR action ?| ARRAY[
                  'node', 'group', 'tag', 'topic', 'payload', 'command',
                  'entity_id', 'entity', 'entity_name', 'cooldown'
              ]
          )
    ) THEN
        RAISE EXCEPTION 'rule control actions must use entity_instance_id and value only';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'control'
    ) AND NEW.jdm_content->'_config' ? 'sourceNodeIds' THEN
        RAISE EXCEPTION 'rule control actions cannot read physical node sources';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(actions) AS action
        WHERE action->>'type' = 'control'
    ) AND NOT (
        EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(NEW.jdm_content->'_config'->'sourceEntityInstanceIds') = 'array'
                    THEN NEW.jdm_content->'_config'->'sourceEntityInstanceIds'
                    ELSE '[]'::jsonb END
            ) AS source_id
            WHERE btrim(source_id) <> ''
        )
        OR EXISTS (
            SELECT 1
            FROM jsonb_each(
                CASE WHEN jsonb_typeof(NEW.jdm_content->'_config'->'inputMappings') = 'object'
                    THEN NEW.jdm_content->'_config'->'inputMappings'
                    ELSE '{}'::jsonb END
            ) AS mapping(field, source_id)
            WHERE jsonb_typeof(source_id) = 'string'
              AND btrim(source_id #>> '{}') <> ''
        )
    ) THEN
        RAISE EXCEPTION 'rule control actions require entity instance inputs';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT action->>'id' AS action_id
            FROM jsonb_array_elements(actions) AS action
            WHERE action->>'type' = 'control'
        ) AS control_actions
        GROUP BY action_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'rule control action identifiers must be unique';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_rules_unsafe_control_actions_read_only ON t_rules;
CREATE TRIGGER trg_rules_unsafe_control_actions_read_only
BEFORE INSERT OR UPDATE ON t_rules
FOR EACH ROW EXECUTE FUNCTION reject_unsafe_rule_control_actions();

COMMIT;
