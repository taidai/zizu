-- Schema 052: committed-L2 JDM model revisions and immutable execution facts.

BEGIN;

DO $migration$
DECLARE
  footprint INTEGER;
  contract_valid BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-052'));

  IF to_regclass('public.t_rules') IS NULL
     OR to_regclass('public.t_data_frames') IS NULL
     OR to_regclass('public.t_configuration_state') IS NULL
     OR to_regclass('public.t_configuration_revisions') IS NULL
     OR to_regclass('public.t_committed_frame_consumers') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_052_REQUIRES_051: committed frame contract is missing';
  END IF;

  SELECT count(*) INTO footprint
  FROM (VALUES
    (EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public'
        AND table_name='t_rules'
        AND column_name='configuration_revision'
    )),
    (to_regclass('public.t_jdm_executions') IS NOT NULL),
    (EXISTS (
      SELECT 1 FROM pg_indexes
      WHERE schemaname='public'
        AND indexname='ix_jdm_executions_rule_sequence'
    ))
  ) AS objects(present)
  WHERE present;

  IF footprint > 0 THEN
    SELECT footprint = 3
       AND EXISTS (
         SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='t_rules'
           AND column_name='configuration_revision'
           AND data_type='bigint'
           AND is_nullable='NO'
       )
       AND EXISTS (
         SELECT 1 FROM pg_constraint
         WHERE conrelid='public.t_rules'::regclass
           AND conname='fk_rule_configuration_revision'
       )
       AND NOT EXISTS (
         SELECT required.column_name
         FROM (VALUES
           ('id'),('rule_id'),('rule_version'),('frame_id'),
           ('frame_sequence'),('configuration_revision'),('model_digest'),
           ('status'),('reason_code'),('inputs'),('outputs'),('actions'),
           ('executed_at')
         ) AS required(column_name)
         LEFT JOIN information_schema.columns AS actual
           ON actual.table_schema='public'
          AND actual.table_name='t_jdm_executions'
          AND actual.column_name=required.column_name
         WHERE actual.column_name IS NULL
       )
       AND (
         SELECT count(*) FROM pg_constraint
         WHERE conrelid='public.t_jdm_executions'::regclass
           AND conname IN (
             't_jdm_executions_pkey',
             'uq_jdm_execution_rule_frame',
             'fk_jdm_execution_frame',
             'fk_jdm_execution_configuration_revision',
             'chk_jdm_execution_rule_version',
             'chk_jdm_execution_frame_sequence',
             'chk_jdm_execution_model_digest',
             'chk_jdm_execution_status',
             'chk_jdm_execution_reason'
           )
       ) = 9
      INTO contract_valid;

    IF contract_valid THEN
      RETURN;
    END IF;
    RAISE EXCEPTION 'SCHEMA_052_PARTIAL_STRUCTURE: damaged JDM execution schema';
  END IF;

  ALTER TABLE public.t_rules
    ADD COLUMN configuration_revision BIGINT;

  UPDATE public.t_rules
  SET configuration_revision=(
    SELECT current_revision
    FROM public.t_configuration_state
    WHERE singleton=TRUE
  );

  ALTER TABLE public.t_rules
    ALTER COLUMN configuration_revision SET NOT NULL,
    ADD CONSTRAINT fk_rule_configuration_revision
      FOREIGN KEY(configuration_revision)
      REFERENCES public.t_configuration_revisions(revision);

  CREATE TABLE public.t_jdm_executions (
    id UUID PRIMARY KEY,
    rule_id UUID NOT NULL,
    rule_version INTEGER NOT NULL
      CONSTRAINT chk_jdm_execution_rule_version CHECK (rule_version > 0),
    frame_id UUID NOT NULL
      CONSTRAINT fk_jdm_execution_frame
      REFERENCES public.t_data_frames(frame_id) ON DELETE CASCADE,
    frame_sequence BIGINT NOT NULL
      CONSTRAINT chk_jdm_execution_frame_sequence CHECK (frame_sequence > 0),
    configuration_revision BIGINT NOT NULL
      CONSTRAINT fk_jdm_execution_configuration_revision
      REFERENCES public.t_configuration_revisions(revision),
    model_digest TEXT NOT NULL
      CONSTRAINT chk_jdm_execution_model_digest
      CHECK (model_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL
      CONSTRAINT chk_jdm_execution_status
      CHECK (status IN ('executed','rejected')),
    reason_code TEXT,
    inputs JSONB NOT NULL,
    outputs JSONB NOT NULL,
    actions JSONB NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_jdm_execution_rule_frame
      UNIQUE(rule_id,rule_version,frame_id),
    CONSTRAINT chk_jdm_execution_reason CHECK (
      (status='executed' AND reason_code IS NULL)
      OR (status='rejected' AND reason_code IS NOT NULL)
    )
  );

  CREATE INDEX ix_jdm_executions_rule_sequence
    ON public.t_jdm_executions(rule_id,frame_sequence DESC);
END
$migration$;

COMMIT;
