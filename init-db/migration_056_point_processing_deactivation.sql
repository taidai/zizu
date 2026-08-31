-- Schema 056: allow reviewed point-processing deactivation while preserving evidence.

BEGIN;

DO $migration$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-056'));

  IF to_regclass('public.t_installed_point_processings') IS NULL
     OR to_regclass('public.t_point_processing_output_bindings') IS NULL
     OR to_regclass('public.t_entity_instances') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_056_REQUIRES_POINT_PROCESSING_RUNTIME';
  END IF;
END
$migration$;

CREATE OR REPLACE FUNCTION public.assert_entity_instance_single_source(target_id UUID)
RETURNS void LANGUAGE plpgsql AS $function$
DECLARE
  kind TEXT;
  entity_active BOOLEAN;
  active_outputs INTEGER;
BEGIN
  SELECT source_kind, active INTO kind, entity_active
  FROM public.t_entity_instances
  WHERE id = target_id;

  IF kind IS NULL OR entity_active IS FALSE THEN
    RETURN;
  END IF;

  SELECT count(*) INTO active_outputs
  FROM public.t_point_processing_output_bindings AS binding
  JOIN public.t_installed_point_processings AS installed
    ON installed.id = binding.installed_processing_id
  WHERE binding.entity_instance_id = target_id
    AND installed.current = TRUE;

  IF kind = 'point_processing' AND active_outputs <> 1 THEN
    RAISE EXCEPTION
      'active point processing entity must have exactly one current processing source'
      USING ERRCODE = '23514';
  END IF;
END;
$function$;

COMMIT;
