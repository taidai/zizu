-- EN9 runtime provenance and window-bounded acceptance evidence.
BEGIN;

CREATE TABLE IF NOT EXISTS t_runtime_health_samples (
  runtime_instance_id UUID NOT NULL REFERENCES t_runtime_instances(id),
  sampled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  pipeline_running BOOLEAN NOT NULL,
  mqtt_connected BOOLEAN NOT NULL,
  last_message_at TIMESTAMPTZ,
  PRIMARY KEY (runtime_instance_id, sampled_at)
);

ALTER TABLE t_l2_observations
  ADD COLUMN IF NOT EXISTS producing_runtime_instance_id UUID
    REFERENCES t_runtime_instances(id);

ALTER TABLE t_l2_latest
  ADD COLUMN IF NOT EXISTS producing_runtime_instance_id UUID
    REFERENCES t_runtime_instances(id);

CREATE INDEX IF NOT EXISTS ix_runtime_health_samples_window
  ON t_runtime_health_samples(runtime_instance_id, sampled_at);

CREATE INDEX IF NOT EXISTS ix_l2_observations_acceptance_window
  ON t_l2_observations(
    entity_instance_id,
    processing_revision_id,
    site_configuration_version,
    calculated_at DESC
  );

CREATE INDEX IF NOT EXISTS ix_l2_observations_observed_window
  ON t_l2_observations(entity_instance_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_telemetry_observation_time
  ON t_telemetry(observation_id, ts);

CREATE INDEX IF NOT EXISTS ix_l2_sources_event_kind
  ON t_l2_observation_sources(
    l2_event_id,
    l2_observed_at,
    source_kind,
    l0_observation_id
  );

DROP TRIGGER IF EXISTS trg_t_runtime_health_samples_append_only
  ON t_runtime_health_samples;
CREATE TRIGGER trg_t_runtime_health_samples_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON t_runtime_health_samples
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

COMMIT;
