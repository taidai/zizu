-- Schema 058: no active L2 entity may remain on a retired physical node.

BEGIN;

DO $migration$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-058'));

  IF to_regclass('public.t_nodes') IS NULL
     OR to_regclass('public.t_entity_instances') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_058_REQUIRES_NODE_ENTITY_RUNTIME';
  END IF;
END
$migration$;

UPDATE public.t_entity_instances AS entity
SET active=FALSE,updated_at=clock_timestamp()
FROM public.t_nodes AS node
WHERE node.id=entity.node_id
  AND node.retired_at IS NOT NULL
  AND entity.active=TRUE;

COMMIT;
