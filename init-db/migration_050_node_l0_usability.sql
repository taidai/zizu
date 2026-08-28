-- Schema 050: maintainable real-node tree and stable L0 source identity.

BEGIN;

SELECT pg_advisory_xact_lock(hashtext('zizu-schema-050'));

DO $migration$
BEGIN
  IF to_regclass('public.t_nodes') IS NULL
     OR to_regclass('public.t_tags') IS NULL
     OR to_regclass('public.t_configuration_state') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_050_REQUIRES_NODE_DATA_TRUNK';
  END IF;
END
$migration$;

ALTER TABLE public.t_nodes
  ALTER COLUMN id SET DEFAULT gen_random_uuid(),
  ADD COLUMN IF NOT EXISTS layer SMALLINT,
  ADD COLUMN IF NOT EXISTS config JSONB,
  ADD COLUMN IF NOT EXISTS sort_order INTEGER,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS retired_by TEXT;

UPDATE public.t_nodes
SET config=COALESCE(config,'{}'::jsonb),
    sort_order=COALESCE(sort_order,0),
    created_at=COALESCE(created_at,clock_timestamp()),
    updated_at=COALESCE(updated_at,clock_timestamp());

WITH RECURSIVE tree(id,depth,path) AS (
  SELECT id,1,ARRAY[id]
  FROM public.t_nodes
  WHERE parent_id IS NULL
  UNION ALL
  SELECT child.id,parent.depth+1,parent.path || child.id
  FROM public.t_nodes AS child
  JOIN tree AS parent ON child.parent_id=parent.id
  WHERE NOT child.id=ANY(parent.path)
)
UPDATE public.t_nodes AS node
SET layer=tree.depth
FROM tree
WHERE tree.id=node.id;

DO $migration$
BEGIN
  IF EXISTS (SELECT 1 FROM public.t_nodes WHERE layer IS NULL OR layer NOT BETWEEN 1 AND 5) THEN
    RAISE EXCEPTION 'SCHEMA_050_NODE_TREE_INVALID: every node must be reachable within five levels';
  END IF;
  IF EXISTS (SELECT 1 FROM public.t_nodes WHERE parent_id=id) THEN
    RAISE EXCEPTION 'SCHEMA_050_NODE_TREE_INVALID: a node cannot parent itself';
  END IF;
END
$migration$;

ALTER TABLE public.t_nodes
  ALTER COLUMN layer SET NOT NULL,
  ALTER COLUMN config SET DEFAULT '{}'::jsonb,
  ALTER COLUMN config SET NOT NULL,
  ALTER COLUMN sort_order SET DEFAULT 0,
  ALTER COLUMN sort_order SET NOT NULL,
  ALTER COLUMN created_at SET DEFAULT clock_timestamp(),
  ALTER COLUMN created_at SET NOT NULL,
  ALTER COLUMN updated_at SET DEFAULT clock_timestamp(),
  ALTER COLUMN updated_at SET NOT NULL;

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.t_nodes'::regclass
      AND conname='chk_nodes_layer'
  ) THEN
    ALTER TABLE public.t_nodes
      ADD CONSTRAINT chk_nodes_layer CHECK (layer BETWEEN 1 AND 5);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.t_nodes'::regclass
      AND conname='chk_nodes_retirement'
  ) THEN
    ALTER TABLE public.t_nodes
      ADD CONSTRAINT chk_nodes_retirement
      CHECK (retired_at IS NULL OR enabled=FALSE);
  END IF;
END
$migration$;

CREATE INDEX IF NOT EXISTS ix_nodes_active_parent
  ON public.t_nodes(parent_id,sort_order,name)
  WHERE retired_at IS NULL;

DO $migration$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.t_tags
    WHERE source_type IS NOT NULL
      AND btrim(source_type) <> ''
      AND source_path IS NOT NULL
      AND btrim(source_path) <> ''
    GROUP BY node_id,lower(source_type),source_path
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'SCHEMA_050_DUPLICATE_L0_SOURCE_IDENTITY';
  END IF;
END
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_node_source_identity
  ON public.t_tags(node_id,lower(source_type),source_path)
  WHERE source_type IS NOT NULL
    AND btrim(source_type) <> ''
    AND source_path IS NOT NULL
    AND btrim(source_path) <> '';

COMMIT;
