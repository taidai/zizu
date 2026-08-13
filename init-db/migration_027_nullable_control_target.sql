-- An unmappable compatibility request is rejected evidence, not a synthetic
-- entity-instance reference.
BEGIN;

ALTER TABLE t_control_commands
    ALTER COLUMN entity_instance_id DROP NOT NULL;

COMMIT;
