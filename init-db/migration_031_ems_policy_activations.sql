-- Explicit engineer activation prevents a newly installed policy from running
-- until its confirmed input has passed a live freshness and quality check.
CREATE TABLE IF NOT EXISTS t_ems_policy_activations (
    site_configuration_version INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    enabled_by TEXT NOT NULL,
    enabled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_configuration_version, policy_id),
    CONSTRAINT t_ems_policy_activations_version_positive
        CHECK (site_configuration_version > 0),
    CONSTRAINT t_ems_policy_activations_policy_id_nonempty
        CHECK (length(trim(policy_id)) > 0)
);
