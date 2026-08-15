BEGIN;

ALTER TABLE t_alarm_configuration_plans
    ADD COLUMN IF NOT EXISTS application_id UUID;

-- migration_034 permits exactly ready -> applied. Validate the immutable
-- evidence before temporarily removing that guard for this one-time backfill.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM t_alarm_configuration_plans
        WHERE status = 'applied'
          AND (
              jsonb_typeof(applied_result) IS DISTINCT FROM 'object'
              OR jsonb_typeof(applied_result->'id') IS DISTINCT FROM 'string'
              OR jsonb_typeof(applied_result->'plan_id') IS DISTINCT FROM 'string'
              OR (applied_result->>'id') !~
                 '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
              OR applied_result->>'plan_id' IS DISTINCT FROM id::text
              OR (
                  application_id IS NOT NULL
                  AND applied_result->>'id' IS DISTINCT FROM application_id::text
              )
          )
    ) THEN
        RAISE EXCEPTION
            'invalid existing applied alarm configuration evidence';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_append_only
    ON t_alarm_configuration_plans;

UPDATE t_alarm_configuration_plans
SET application_id = (applied_result->>'id')::uuid
WHERE status = 'applied'
  AND application_id IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_alarm_configuration_plan_application'
    ) THEN
        ALTER TABLE t_alarm_configuration_plans
            ADD CONSTRAINT uq_alarm_configuration_plan_application
            UNIQUE (application_id);
    END IF;
END;
$$;

ALTER TABLE t_alarm_configuration_plans
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_plan_application,
    ADD CONSTRAINT chk_alarm_configuration_plan_application CHECK (
        (status = 'applied'
         AND application_id IS NOT NULL
         AND application_id = (applied_result->>'id')::uuid
         AND id = (applied_result->>'plan_id')::uuid)
        OR (status <> 'applied' AND application_id IS NULL)
    );

CREATE TRIGGER trg_alarm_configuration_plans_append_only
    BEFORE UPDATE OR DELETE ON t_alarm_configuration_plans
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_configuration_plan_append_only();

CREATE TABLE IF NOT EXISTS t_alarm_configuration_reports (
    id UUID PRIMARY KEY,
    application_id UUID NOT NULL
        REFERENCES t_alarm_configuration_plans(application_id),
    installation_id UUID NOT NULL REFERENCES t_solution_installations(id),
    site_configuration_version BIGINT NOT NULL
        REFERENCES t_site_configuration_versions(version),
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    report JSONB NOT NULL,
    digest CHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_alarm_configuration_report_actor_nonempty
        CHECK (length(btrim(actor)) > 0),
    CONSTRAINT chk_alarm_configuration_report_status
        CHECK (status IN ('passed', 'failed')),
    CONSTRAINT chk_alarm_configuration_report_digest_sha256
        CHECK (digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_alarm_configuration_report_time_order
        CHECK (finished_at >= started_at),
    CONSTRAINT chk_alarm_configuration_report_identity CHECK (
        report->>'id' IS NOT DISTINCT FROM id::text
        AND report->>'application_id' IS NOT DISTINCT FROM application_id::text
        AND report->>'installation_id' IS NOT DISTINCT FROM installation_id::text
        AND (report->>'site_configuration_version')::bigint
            IS NOT DISTINCT FROM site_configuration_version
        AND report->>'actor' IS NOT DISTINCT FROM actor
        AND report->>'status' IS NOT DISTINCT FROM status
        AND report->>'digest' IS NOT DISTINCT FROM btrim(digest)
        AND (report->>'started_at')::timestamptz IS NOT DISTINCT FROM started_at
        AND (report->>'finished_at')::timestamptz IS NOT DISTINCT FROM finished_at
        AND jsonb_typeof(report->'items') = 'array'
    )
);

CREATE INDEX IF NOT EXISTS idx_alarm_configuration_reports_application
    ON t_alarm_configuration_reports (application_id, finished_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_configuration_reports_site_version
    ON t_alarm_configuration_reports
       (site_configuration_version DESC, finished_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS t_alarm_configuration_acceptance_idempotency (
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    report_id UUID NOT NULL REFERENCES t_alarm_configuration_reports(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_alarm_configuration_acceptance_idempotency
        PRIMARY KEY (actor, idempotency_key),
    CONSTRAINT chk_alarm_configuration_acceptance_actor_nonempty
        CHECK (length(btrim(actor)) > 0),
    CONSTRAINT chk_alarm_configuration_acceptance_key_nonempty
        CHECK (length(btrim(idempotency_key)) > 0),
    CONSTRAINT chk_alarm_configuration_acceptance_request_digest
        CHECK (request_digest ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION reject_alarm_configuration_report_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'alarm configuration acceptance evidence is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_configuration_reports_append_only
    ON t_alarm_configuration_reports;
CREATE TRIGGER trg_alarm_configuration_reports_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_configuration_reports
    FOR EACH STATEMENT
    EXECUTE FUNCTION reject_alarm_configuration_report_mutation();

DROP TRIGGER IF EXISTS trg_alarm_configuration_acceptance_idempotency_append_only
    ON t_alarm_configuration_acceptance_idempotency;
CREATE TRIGGER trg_alarm_configuration_acceptance_idempotency_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE
    ON t_alarm_configuration_acceptance_idempotency
    FOR EACH STATEMENT
    EXECUTE FUNCTION reject_alarm_configuration_report_mutation();

COMMENT ON TABLE t_alarm_configuration_reports IS
    'Immutable observer-only evidence for one applied alarm configuration';
COMMENT ON TABLE t_alarm_configuration_acceptance_idempotency IS
    'Actor-scoped acceptance request digest and immutable report binding';

COMMIT;
