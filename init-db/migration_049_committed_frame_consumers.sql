-- Schema 049: transactional receipts for stateful committed-frame consumers.

BEGIN;

DO $migration$
DECLARE
  footprint INTEGER;
  contract_valid BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-049'));

  IF to_regclass('public.t_data_frames') IS NULL
     OR to_regclass('public.t_data_frame_outbox') IS NULL
     OR to_regclass('public.l2_agg_1h') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_049_REQUIRES_048: frame retention contract is missing';
  END IF;

  SELECT count(*) INTO footprint
  FROM (VALUES
    (to_regclass('public.t_committed_frame_consumers') IS NOT NULL),
    (EXISTS (
      SELECT 1 FROM pg_indexes
      WHERE schemaname='public'
        AND indexname='uq_committed_frame_consumer_sequence'
    ))
  ) AS objects(present)
  WHERE present;

  IF footprint > 0 THEN
    SELECT footprint = 2
       AND NOT EXISTS (
         SELECT required.column_name
         FROM (VALUES
           ('consumer_key'),('frame_id'),('frame_sequence'),
           ('configuration_revision'),('consumed_at')
         ) AS required(column_name)
         LEFT JOIN information_schema.columns AS actual
           ON actual.table_schema='public'
          AND actual.table_name='t_committed_frame_consumers'
          AND actual.column_name=required.column_name
         WHERE actual.column_name IS NULL
       )
       AND EXISTS (
         SELECT 1 FROM pg_constraint
         WHERE conrelid='public.t_committed_frame_consumers'::regclass
           AND conname='t_committed_frame_consumers_pkey'
       )
       AND (
         SELECT count(*) FROM pg_constraint
         WHERE conrelid='public.t_committed_frame_consumers'::regclass
           AND conname IN (
             'chk_committed_frame_consumer_key',
             'chk_committed_frame_consumer_sequence',
             'chk_committed_frame_consumer_revision',
             'fk_committed_frame_consumer_frame'
           )
       ) = 4
      INTO contract_valid;
    IF contract_valid THEN
      RETURN;
    END IF;
    RAISE EXCEPTION 'SCHEMA_049_PARTIAL_STRUCTURE: damaged consumer receipt schema';
  END IF;

  CREATE TABLE public.t_committed_frame_consumers (
    consumer_key TEXT NOT NULL
      CONSTRAINT chk_committed_frame_consumer_key
      CHECK (consumer_key IN ('alarm','jdm','automatic_control')),
    frame_id UUID NOT NULL
      CONSTRAINT fk_committed_frame_consumer_frame
      REFERENCES public.t_data_frames(frame_id) ON DELETE CASCADE,
    frame_sequence BIGINT NOT NULL
      CONSTRAINT chk_committed_frame_consumer_sequence CHECK (frame_sequence > 0),
    configuration_revision BIGINT NOT NULL
      CONSTRAINT chk_committed_frame_consumer_revision
      CHECK (configuration_revision >= 0),
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer_key,frame_id)
  );

  CREATE UNIQUE INDEX uq_committed_frame_consumer_sequence
    ON public.t_committed_frame_consumers(consumer_key,frame_sequence);
END
$migration$;

COMMIT;
