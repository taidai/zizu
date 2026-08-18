-- PCS data-trunk contract gate.  This migration is intentionally contract-only:
-- migration_038 remains the recorded expand step.

CREATE OR REPLACE FUNCTION assert_entity_instance_single_source(target_id UUID)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  kind TEXT;
  active_tags INTEGER;
  active_outputs INTEGER;
BEGIN
  SELECT source_kind INTO kind
  FROM t_entity_instances
  WHERE id = target_id;

  IF kind IS NULL THEN
    RETURN;
  END IF;

  SELECT count(*) INTO active_tags
  FROM t_entity_instance_bindings
  WHERE entity_instance_id = target_id
    AND active = TRUE;

  SELECT count(*) INTO active_outputs
  FROM t_conversion_output_bindings AS binding
  JOIN t_installed_point_conversions AS installed
    ON installed.id = binding.installed_conversion_id
  WHERE binding.entity_instance_id = target_id
    AND installed.current = TRUE;

  IF kind = 'point_conversion'
     AND (active_tags <> 0 OR active_outputs <> 1) THEN
    RAISE EXCEPTION
      'point conversion entity must have exactly one conversion source and no direct tag source'
      USING ERRCODE = '23514';
  END IF;

  IF kind = 'legacy_tag' AND active_outputs <> 0 THEN
    RAISE EXCEPTION
      'legacy entity cannot have a point conversion source'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_entity_instance_binding_source()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_entity_instance_single_source(
    CASE WHEN TG_OP = 'DELETE' THEN OLD.entity_instance_id
         ELSE NEW.entity_instance_id END
  );
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_entity_instance_kind_source()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_entity_instance_single_source(
    CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END
  );
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_installed_conversion_sources()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  target_id UUID;
  installed_id UUID;
BEGIN
  installed_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
  FOR target_id IN
    SELECT DISTINCT entity_instance_id
    FROM t_conversion_output_bindings
    WHERE installed_conversion_id = installed_id
  LOOP
    PERFORM assert_entity_instance_single_source(target_id);
  END LOOP;
  RETURN NULL;
END;
$$;

DO $$
DECLARE
  target_id UUID;
BEGIN
  FOR target_id IN SELECT id FROM t_entity_instances LOOP
    PERFORM assert_entity_instance_single_source(target_id);
  END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_entity_instance_binding_single_source
  ON t_entity_instance_bindings;
CREATE CONSTRAINT TRIGGER trg_entity_instance_binding_single_source
AFTER INSERT OR UPDATE OR DELETE ON t_entity_instance_bindings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_binding_source();

DROP TRIGGER IF EXISTS trg_conversion_output_binding_single_source
  ON t_conversion_output_bindings;
CREATE CONSTRAINT TRIGGER trg_conversion_output_binding_single_source
AFTER INSERT OR UPDATE OR DELETE ON t_conversion_output_bindings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_binding_source();

DROP TRIGGER IF EXISTS trg_entity_instance_kind_single_source
  ON t_entity_instances;
CREATE CONSTRAINT TRIGGER trg_entity_instance_kind_single_source
AFTER INSERT OR UPDATE OF source_kind ON t_entity_instances
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_kind_source();

DROP TRIGGER IF EXISTS trg_installed_conversion_single_source
  ON t_installed_point_conversions;
CREATE CONSTRAINT TRIGGER trg_installed_conversion_single_source
AFTER INSERT OR UPDATE OF current OR DELETE ON t_installed_point_conversions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_installed_conversion_sources();
