-- Allow whole-strategy deletion, not edits/deletion of individual published evidence.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('zizu-schema-064'));

ALTER TABLE public.t_dispatch_strategy_owners
  DROP CONSTRAINT IF EXISTS t_dispatch_strategy_owners_strategy_id_fkey,
  ADD CONSTRAINT t_dispatch_strategy_owners_strategy_id_fkey
    FOREIGN KEY(strategy_id) REFERENCES public.t_dispatch_strategies(id) ON DELETE CASCADE;
ALTER TABLE public.t_dispatch_control_intents
  DROP CONSTRAINT IF EXISTS t_dispatch_control_intents_strategy_id_fkey,
  ADD CONSTRAINT t_dispatch_control_intents_strategy_id_fkey
    FOREIGN KEY(strategy_id) REFERENCES public.t_dispatch_strategies(id) ON DELETE CASCADE;
ALTER TABLE public.t_dispatch_strategy_events
  DROP CONSTRAINT IF EXISTS t_dispatch_strategy_events_strategy_id_fkey,
  ADD CONSTRAINT t_dispatch_strategy_events_strategy_id_fkey
    FOREIGN KEY(strategy_id) REFERENCES public.t_dispatch_strategies(id) ON DELETE CASCADE;

CREATE OR REPLACE FUNCTION public.reject_published_dispatch_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' AND NOT EXISTS (
    SELECT 1 FROM public.t_dispatch_strategies WHERE id=OLD.strategy_id
  ) THEN RETURN OLD; END IF;
  IF OLD.lifecycle='PUBLISHED' THEN
    RAISE EXCEPTION 'published dispatch strategy revisions are immutable';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.reject_dispatch_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' AND NOT EXISTS (
    SELECT 1 FROM public.t_dispatch_strategies WHERE id=OLD.strategy_id
  ) THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'dispatch strategy events are append-only';
END;
$$;
COMMIT;
