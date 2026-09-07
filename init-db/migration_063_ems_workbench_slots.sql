-- Schema 063: auditable manual bindings for the five fixed EMS workbench slots.
BEGIN;

SELECT pg_advisory_xact_lock(hashtext('zizu-schema-063'));

DO $requirements$
BEGIN
  IF to_regclass('public.t_entity_instances') IS NULL
     OR to_regclass('public.t_point_processing_output_bindings') IS NULL
     OR to_regclass('public.t_installed_point_processings') IS NULL
     OR to_regclass('public.t_configuration_revisions') IS NULL
     OR to_regclass('public.t_configuration_state') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_063_REQUIRES_062: L2 or configuration contract is missing'
      USING ERRCODE = '55000';
  END IF;
END
$requirements$;

CREATE TABLE IF NOT EXISTS public.t_ems_workbench_slot_bindings (
  slot_key TEXT PRIMARY KEY
    CONSTRAINT chk_ems_workbench_slot_key CHECK (
      slot_key IN (
        'site-power',
        'pv-power',
        'storage-power',
        'storage-soc',
        'charging-power'
      )
    ),
  entity_instance_id UUID NOT NULL
    REFERENCES public.t_entity_instances(id) ON DELETE RESTRICT,
  configuration_revision BIGINT NOT NULL
    REFERENCES public.t_configuration_revisions(revision),
  created_by TEXT NOT NULL
    CONSTRAINT chk_ems_workbench_slot_created_by CHECK (btrim(created_by) <> ''),
  updated_by TEXT NOT NULL
    CONSTRAINT chk_ems_workbench_slot_updated_by CHECK (btrim(updated_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_ems_workbench_slot_entity
  ON public.t_ems_workbench_slot_bindings(entity_instance_id);

CREATE TABLE IF NOT EXISTS public.t_ems_workbench_slot_idempotency (
  actor TEXT NOT NULL
    CONSTRAINT chk_ems_workbench_slot_idem_actor CHECK (btrim(actor) <> ''),
  idempotency_key TEXT NOT NULL
    CONSTRAINT chk_ems_workbench_slot_idem_key CHECK (
      btrim(idempotency_key) <> '' AND length(idempotency_key) <= 200
    ),
  request_digest CHAR(64) NOT NULL
    CONSTRAINT chk_ems_workbench_slot_idem_digest CHECK (
      request_digest ~ '^[0-9a-f]{64}$'
    ),
  configuration_revision BIGINT NOT NULL
    REFERENCES public.t_configuration_revisions(revision),
  response JSONB NOT NULL
    CONSTRAINT chk_ems_workbench_slot_idem_response CHECK (
      jsonb_typeof(response) = 'object'
    ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(actor, idempotency_key)
);

COMMIT;
