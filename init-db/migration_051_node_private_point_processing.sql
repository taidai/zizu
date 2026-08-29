-- Schema 051: node-private immutable point-processing definitions.

BEGIN;

SELECT pg_advisory_xact_lock(hashtext('zizu-schema-051'));

DO $migration$
BEGIN
  IF to_regclass('public.t_point_processing_templates') IS NULL
     OR to_regclass('public.t_nodes') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_051_REQUIRES_POINT_PROCESSING_AND_NODES';
  END IF;
END
$migration$;

ALTER TABLE public.t_point_processing_templates
  ADD COLUMN IF NOT EXISTS reuse_scope TEXT NOT NULL DEFAULT 'shared',
  ADD COLUMN IF NOT EXISTS owner_node_id UUID REFERENCES public.t_nodes(id);

UPDATE public.t_point_processing_templates
SET reuse_scope='shared', owner_node_id=NULL
WHERE reuse_scope IS NULL;

DO $migration$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.t_point_processing_templates
    WHERE NOT (
      (reuse_scope='shared' AND owner_node_id IS NULL)
      OR (reuse_scope='node' AND owner_node_id IS NOT NULL)
    )
  ) THEN
    RAISE EXCEPTION 'SCHEMA_051_POINT_PROCESSING_SCOPE_INVALID';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid='public.t_point_processing_templates'::regclass
      AND conname='chk_point_processing_template_reuse_scope'
  ) THEN
    ALTER TABLE public.t_point_processing_templates
      ADD CONSTRAINT chk_point_processing_template_reuse_scope
      CHECK (
        (reuse_scope='shared' AND owner_node_id IS NULL)
        OR (reuse_scope='node' AND owner_node_id IS NOT NULL)
      );
  END IF;
END
$migration$;

CREATE INDEX IF NOT EXISTS ix_point_processing_templates_node_scope
  ON public.t_point_processing_templates(owner_node_id,status)
  WHERE reuse_scope='node';

COMMIT;
