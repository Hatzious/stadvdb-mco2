-- The table every server stores. Server 0 (Hera) holds all rows; servers 1 and 2 each hold one fragment.
-- Only 5 of the CSV's 17 columns are kept (see schema/incoming.sql for the full CSV layout).
CREATE TABLE services (
    stop_rdt_id         bigint PRIMARY KEY,  -- unique per row, never empty (checked on the 2021 data)
    service_type        varchar,             -- fragmentation rule: 'Sprinter' -> server 1, the rest -> server 2
    service_company     varchar,             -- filter that spans both fragments
    stop_arrival_time   timestamptz,
    stop_arrival_delay  bigint               -- minutes late vs. the scheduled arrival
);

-- Let the "app" login (README: Network setup, step 6) read and change the rows.
-- Skipped if "app" doesn't exist yet, so this file still works on a fresh server.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON services TO app;
    END IF;
END
$$;
