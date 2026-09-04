-- Schema 061: presentation cleanup without deleting alarm evidence.
BEGIN;

ALTER TABLE public.t_alarm_events
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS archived_by TEXT;

ALTER TABLE public.t_alarm_events
  DROP CONSTRAINT IF EXISTS chk_alarm_event_archive_pair,
  DROP CONSTRAINT IF EXISTS chk_alarm_event_archive_terminal,
  ADD CONSTRAINT chk_alarm_event_archive_pair CHECK (
    (archived_at IS NULL AND archived_by IS NULL)
    OR (archived_at IS NOT NULL AND length(btrim(archived_by)) > 0)
  ),
  ADD CONSTRAINT chk_alarm_event_archive_terminal CHECK (
    archived_at IS NULL OR state = 'recovered'
  );

CREATE INDEX IF NOT EXISTS ix_alarm_events_archive_view
  ON public.t_alarm_events(archived_at, recovered_at DESC, id DESC);

ALTER TABLE public.t_alarm_rule_sets
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS archived_by TEXT;

ALTER TABLE public.t_alarm_rule_sets
  DROP CONSTRAINT IF EXISTS chk_alarm_rule_set_archive_pair,
  ADD CONSTRAINT chk_alarm_rule_set_archive_pair CHECK (
    (archived_at IS NULL AND archived_by IS NULL)
    OR (archived_at IS NOT NULL AND length(btrim(archived_by)) > 0)
  );

COMMENT ON COLUMN public.t_alarm_events.archived_at IS
  'Presentation archive marker; lifecycle and transition evidence remain immutable';
COMMENT ON COLUMN public.t_alarm_rule_sets.archived_at IS
  'Soft-delete marker; immutable rule revisions and applied plans remain retained';

COMMIT;
