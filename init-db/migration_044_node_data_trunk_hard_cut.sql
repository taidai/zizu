-- Schema 044: hard-cut solution delivery to the node-owned L0/L1/L2 trunk.
-- The migration is intentionally fail-closed: ownership is proven before any
-- legacy product table is removed.

BEGIN;

DO $$
DECLARE
  new_tables INTEGER;
  old_tables INTEGER;
BEGIN
  SELECT count(*) INTO new_tables
  FROM (VALUES
    ('t_configuration_state'),
    ('t_configuration_revisions'),
    ('t_configuration_audit')
  ) AS required(name)
  WHERE to_regclass('public.' || required.name) IS NOT NULL;

  SELECT count(*) INTO old_tables
  FROM (VALUES
    ('t_solution_packages'),
    ('t_site_configuration_versions'),
    ('t_device_instances')
  ) AS legacy(name)
  WHERE to_regclass('public.' || legacy.name) IS NOT NULL;

  IF new_tables > 0 AND (new_tables <> 3 OR old_tables > 0) THEN
    RAISE EXCEPTION 'SCHEMA_044_PARTIAL_STRUCTURE: schema 044 is malformed'
      USING ERRCODE = '55000';
  END IF;

  IF new_tables = 0 AND old_tables <> 3 THEN
    RAISE EXCEPTION 'SCHEMA_044_PARTIAL_STRUCTURE: complete schema 043 is required'
      USING ERRCODE = '55000';
  END IF;
END;
$$;

DO $$
BEGIN
  IF to_regclass('public.t_configuration_state') IS NOT NULL THEN
    RETURN;
  END IF;

  ALTER TABLE public.t_entity_instances
    ADD COLUMN node_id UUID;

  UPDATE public.t_entity_instances AS entity
  SET node_id = device.node_id
  FROM public.t_device_instances AS device
  WHERE device.id = entity.device_instance_id;

  IF EXISTS (
    SELECT 1
    FROM public.t_entity_instances
    WHERE node_id IS NULL
  ) OR EXISTS (
    SELECT 1
    FROM public.t_entity_instances
    WHERE active = TRUE
    GROUP BY node_id, definition_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'HARD_CUT_ENTITY_NODE_AMBIGUOUS: every L2 entity must map to one node'
      USING ERRCODE = '55000';
  END IF;

  CREATE TABLE public.t_configuration_revisions (
    revision BIGINT PRIMARY KEY CHECK (revision >= 0),
    previous_revision BIGINT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    before_digest CHAR(64),
    after_digest CHAR(64) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (previous_revision)
      REFERENCES public.t_configuration_revisions(revision)
  );

  INSERT INTO public.t_configuration_revisions
    (revision, previous_revision, actor, action, resource_kind, resource_id,
     before_digest, after_digest, details, created_at)
  SELECT version, previous_version, actor,
         CASE WHEN version = 0 THEN 'configuration.bootstrap'
              ELSE 'legacy.configuration.publish' END,
         'site', 'single-site', NULL,
         COALESCE(configuration_digest, package_digest, repeat('0', 64)),
         jsonb_build_object(
           'legacy_installation_id', installation_id,
           'legacy_package_record_id', package_record_id
         ),
         created_at
  FROM public.t_site_configuration_versions
  ORDER BY version;

  CREATE TABLE public.t_configuration_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    current_revision BIGINT NOT NULL
      REFERENCES public.t_configuration_revisions(revision)
  );

  INSERT INTO public.t_configuration_state(singleton, current_revision)
  SELECT singleton, current_version
  FROM public.t_site_configuration_state;

  CREATE TABLE public.t_configuration_audit (
    id UUID PRIMARY KEY,
    configuration_revision BIGINT NOT NULL
      REFERENCES public.t_configuration_revisions(revision),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    before_digest CHAR(64),
    after_digest CHAR(64) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );

  INSERT INTO public.t_configuration_audit
    (id, configuration_revision, actor, action, resource_kind, resource_id,
     before_digest, after_digest, details, created_at)
  SELECT id, site_configuration_version, actor,
         'legacy.solution.install', 'site', 'single-site', NULL,
         package_digest, details, created_at
  FROM public.t_solution_delivery_audit;

  ALTER TABLE public.t_entity_instances
    ALTER COLUMN node_id SET NOT NULL;
  ALTER TABLE public.t_entity_instances
    ADD CONSTRAINT fk_entity_instance_node
      FOREIGN KEY(node_id) REFERENCES public.t_nodes(id);
  ALTER TABLE public.t_entity_instances
    DROP COLUMN device_instance_id CASCADE;
  CREATE UNIQUE INDEX uq_entity_instance_node_definition_active
    ON public.t_entity_instances(node_id, definition_id)
    WHERE active = TRUE;

  CREATE TABLE public.t_l2_control_bindings (
    entity_instance_id UUID PRIMARY KEY
      REFERENCES public.t_entity_instances(id),
    l0_tag_id UUID NOT NULL UNIQUE REFERENCES public.t_tags(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  INSERT INTO public.t_l2_control_bindings(entity_instance_id, l0_tag_id, created_at)
  SELECT binding.entity_instance_id, binding.tag_id, binding.created_at
  FROM public.t_entity_instance_bindings AS binding
  JOIN public.t_entity_instances AS entity
    ON entity.id = binding.entity_instance_id
  WHERE binding.active = TRUE
    AND entity.active = TRUE
    AND entity.direction IN ('W', 'RW');

  DROP TABLE IF EXISTS public.t_alarm_configuration_acceptance_idempotency CASCADE;
  DROP TABLE IF EXISTS public.t_alarm_configuration_reports CASCADE;
  DROP TABLE IF EXISTS public.t_cross_node_processing_acceptance_reports CASCADE;
  DROP TABLE IF EXISTS public.t_en9_acceptance_ws_receipts CASCADE;
  DROP TABLE IF EXISTS public.t_en9_acceptance_reports CASCADE;
  DROP TABLE IF EXISTS public.t_runtime_instances CASCADE;

  DROP TABLE IF EXISTS public.t_business_metric_acceptance_reports CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_audit CASCADE;
  DROP TABLE IF EXISTS public.t_entity_capability_contracts CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_recomputations CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_window_results CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_projections CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_source_bindings CASCADE;
  DROP TABLE IF EXISTS public.t_installed_business_metrics CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_plan_items CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_installation_plans CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_revisions CASCADE;
  DROP TABLE IF EXISTS public.t_business_metric_templates CASCADE;

  IF EXISTS (
    SELECT 1 FROM public.t_installed_point_processings
    WHERE processing_scope = 'business_metric'
  ) THEN
    RAISE EXCEPTION
      'HARD_CUT_BUSINESS_METRIC_RUNTIME_PRESENT: export before hard cut'
      USING ERRCODE = '55000';
  END IF;
  DROP INDEX IF EXISTS public.uq_installed_business_metric_processing_current;
  ALTER TABLE public.t_installed_point_processings
    DROP CONSTRAINT IF EXISTS chk_installed_point_processing_scope,
    DROP COLUMN IF EXISTS processing_owner_key,
    DROP COLUMN IF EXISTS processing_scope;
  ALTER TABLE public.t_point_processing_revisions
    DROP CONSTRAINT IF EXISTS chk_point_processing_revision_internal_kind,
    DROP COLUMN IF EXISTS internal_kind;
  DROP INDEX IF EXISTS public.uq_installed_point_processing_current;
  CREATE UNIQUE INDEX uq_installed_point_processing_current
    ON public.t_installed_point_processings(node_id)
    WHERE current = TRUE;

  DROP TABLE IF EXISTS public.t_entity_failover_audit CASCADE;
  DROP TABLE IF EXISTS public.t_entity_failover_policies CASCADE;
  DROP TABLE IF EXISTS public.t_entity_instance_bindings CASCADE;
  DROP TABLE IF EXISTS public.t_entity_binding_confirmations CASCADE;
  DROP TABLE IF EXISTS public.t_solution_point_processing_assets CASCADE;
  DROP TABLE IF EXISTS public.t_ems_policy_activations CASCADE;
  DROP TABLE IF EXISTS public.t_release_locks CASCADE;
  DROP TABLE IF EXISTS public.t_delivery_idempotency CASCADE;
  DROP TABLE IF EXISTS public.t_delivery_reports CASCADE;
  DROP TABLE IF EXISTS public.t_solution_delivery_audit CASCADE;

  DROP TABLE IF EXISTS public.t_site_configuration_parameters CASCADE;
  DROP TABLE IF EXISTS public.t_site_configuration_parameter_values CASCADE;
  DROP TABLE IF EXISTS public.t_solution_installations CASCADE;
  DROP TABLE IF EXISTS public.t_solution_install_plans CASCADE;
  DROP TABLE IF EXISTS public.t_solution_package_assets CASCADE;
  DROP TABLE IF EXISTS public.t_solution_packages CASCADE;
  DROP TABLE IF EXISTS public.t_device_instances CASCADE;
  DROP TABLE IF EXISTS public.t_site_configuration_state CASCADE;
  DROP TABLE IF EXISTS public.t_site_configuration_versions CASCADE;

  ALTER TABLE public.t_point_processing_plans
    DROP COLUMN IF EXISTS entity_identity_installation_id,
    DROP COLUMN IF EXISTS solution_installation_id;
  ALTER TABLE public.t_point_processing_plans
    RENAME COLUMN base_site_configuration_version TO base_configuration_revision;
  ALTER TABLE public.t_point_processing_plans
    ADD CONSTRAINT fk_point_processing_plan_configuration_revision
      FOREIGN KEY(base_configuration_revision)
      REFERENCES public.t_configuration_revisions(revision);

  ALTER TABLE public.t_installed_point_processings
    DROP COLUMN IF EXISTS solution_installation_id;
  ALTER TABLE public.t_installed_point_processings
    RENAME COLUMN site_configuration_version TO configuration_revision;
  ALTER TABLE public.t_installed_point_processings
    ADD CONSTRAINT fk_installed_processing_configuration_revision
      FOREIGN KEY(configuration_revision)
      REFERENCES public.t_configuration_revisions(revision);

  ALTER TABLE public.t_point_processing_applications
    DROP COLUMN IF EXISTS solution_installation_id;
  ALTER TABLE public.t_point_processing_applications
    RENAME COLUMN site_configuration_version TO configuration_revision;
  ALTER TABLE public.t_point_processing_applications
    ADD CONSTRAINT fk_point_processing_application_configuration_revision
      FOREIGN KEY(configuration_revision)
      REFERENCES public.t_configuration_revisions(revision);

  ALTER TABLE public.t_l2_observations
    RENAME COLUMN site_configuration_version TO configuration_revision;
  ALTER TABLE public.t_l2_latest
    RENAME COLUMN site_configuration_version TO configuration_revision;

  ALTER TABLE public.t_alarm_configuration_plans
    RENAME COLUMN base_site_configuration_version TO base_configuration_revision;
  ALTER TABLE public.t_alarm_configuration_plans
    ADD CONSTRAINT fk_alarm_plan_configuration_revision
      FOREIGN KEY(base_configuration_revision)
      REFERENCES public.t_configuration_revisions(revision);
END;
$$;

COMMIT;
