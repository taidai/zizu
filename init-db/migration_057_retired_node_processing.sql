-- Schema 057: retired nodes leave no active L1/L2 runtime work.

BEGIN;

DO $migration$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-057'));

  IF to_regclass('public.t_nodes') IS NULL
     OR to_regclass('public.t_installed_point_processings') IS NULL
     OR to_regclass('public.t_point_processing_output_bindings') IS NULL
     OR to_regclass('public.t_entity_instances') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_057_REQUIRES_NODE_POINT_PROCESSING_RUNTIME';
  END IF;
END
$migration$;

WITH retired_installations AS MATERIALIZED (
  SELECT installed.id
  FROM public.t_installed_point_processings AS installed
  JOIN public.t_nodes AS node ON node.id=installed.node_id
  WHERE installed.current=TRUE
    AND node.retired_at IS NOT NULL
), retired_outputs AS (
  SELECT DISTINCT binding.entity_instance_id
  FROM public.t_point_processing_output_bindings AS binding
  JOIN retired_installations AS installed
    ON installed.id=binding.installed_processing_id
)
UPDATE public.t_entity_instances AS entity
SET active=FALSE,updated_at=clock_timestamp()
WHERE entity.id IN (SELECT entity_instance_id FROM retired_outputs)
  AND entity.active=TRUE;

UPDATE public.t_installed_point_processings AS installed
SET current=FALSE
FROM public.t_nodes AS node
WHERE node.id=installed.node_id
  AND node.retired_at IS NOT NULL
  AND installed.current=TRUE;

COMMIT;
