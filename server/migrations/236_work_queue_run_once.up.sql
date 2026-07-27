-- run_once: when true, a cron-scheduled queue fires at its next occurrence
-- only; the drain-to-idle path clears the cron instead of rescheduling.
ALTER TABLE work_queue ADD COLUMN run_once BOOLEAN NOT NULL DEFAULT false;
