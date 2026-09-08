-- Independent load binding; never reuse grid power as load.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('zizu-schema-065'));
ALTER TABLE public.t_ems_workbench_slot_bindings
  DROP CONSTRAINT chk_ems_workbench_slot_key;
ALTER TABLE public.t_ems_workbench_slot_bindings
  ADD CONSTRAINT chk_ems_workbench_slot_key CHECK (slot_key IN (
    'site-power', 'pv-power', 'storage-power', 'storage-soc', 'charging-power', 'load-power'
  ));
COMMIT;
