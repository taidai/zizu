-- Schema 053: find the oldest unfinished frame without scanning terminal history.

BEGIN;

DO $migration$
DECLARE
  index_columns TEXT[];
  index_predicate TEXT;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-053'));

  IF to_regclass('public.t_data_frames') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_053_REQUIRES_052: committed data-frame contract is missing';
  END IF;

  SELECT array_agg(attribute.attname ORDER BY key.ordinality),
         pg_get_expr(definition.indpred, definition.indrelid)
    INTO index_columns, index_predicate
  FROM pg_index AS definition
  JOIN pg_class AS index_relation
    ON index_relation.oid=definition.indexrelid
  JOIN LATERAL unnest(definition.indkey)
       WITH ORDINALITY AS key(attnum,ordinality) ON TRUE
  JOIN pg_attribute AS attribute
    ON attribute.attrelid=definition.indrelid
   AND attribute.attnum=key.attnum
  WHERE definition.indrelid='public.t_data_frames'::regclass
    AND index_relation.relname='ix_data_frames_claim'
  GROUP BY definition.indpred,definition.indrelid;

  IF index_columns IS NULL
     OR index_predicate IS NULL
     OR position('PENDING' IN index_predicate)=0
     OR position('PROCESSING' IN index_predicate)=0 THEN
    RAISE EXCEPTION
      'SCHEMA_053_PARTIAL_STRUCTURE: unfinished-frame index is missing or damaged';
  END IF;

  IF index_columns=ARRAY['frame_sequence']::TEXT[] THEN
    RETURN;
  END IF;

  IF index_columns<>ARRAY['status','lease_until','frame_sequence']::TEXT[] THEN
    RAISE EXCEPTION
      'SCHEMA_053_PARTIAL_STRUCTURE: unfinished-frame index has unknown columns';
  END IF;

  DROP INDEX public.ix_data_frames_claim;
  CREATE INDEX ix_data_frames_claim
    ON public.t_data_frames(frame_sequence)
    WHERE status IN ('PENDING','PROCESSING');
END
$migration$;

COMMIT;
