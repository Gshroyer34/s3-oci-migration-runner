-- Run as bootstrap database administrator locally, after migrations; never grant worker ownership.
CREATE ROLE migration_coordinator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE migration_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON DATABASE migration FROM PUBLIC;
GRANT CONNECT ON DATABASE migration TO migration_coordinator, migration_worker;
-- The planner builds transaction-local batch assignments; workers do not need TEMP.
GRANT TEMPORARY ON DATABASE migration TO migration_coordinator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO migration_coordinator, migration_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO migration_coordinator;
GRANT SELECT ON migrations, batches, inventory_objects, runtime_security, worker_attempts, object_evidence, worker_heartbeats, queue_events TO migration_worker;
GRANT UPDATE (scheduled_bytes) ON migrations TO migration_worker;
GRANT INSERT, UPDATE ON worker_attempts, object_evidence, worker_heartbeats TO migration_worker;
GRANT UPDATE ON batches TO migration_worker;
GRANT INSERT ON queue_events TO migration_worker;
GRANT SELECT ON cloud_ui_dispatch, cloud_ui_workers TO migration_worker;
GRANT INSERT, UPDATE ON cloud_ui_workers TO migration_worker;
-- Workers remain trusted execution principals with queue-table write access. A malicious enrolled
-- worker can bypass client-side fencing; move mutation authority behind the coordinator before
-- treating worker roles as a security boundary. No worker rights on inventory/security/audit writes.
