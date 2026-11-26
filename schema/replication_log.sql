-- The replication log: every server keeps one. Each write adds an entry for the server that holds the
-- row's other copy, in the same transaction as the data change. The replay applies pending entries to
-- their target, oldest first, then marks them applied (entries are kept as history, not deleted).
--
-- Safe to run again: IF NOT EXISTS skips what's already there. It does NOT change an existing table;
-- to apply a change to the columns, drop the table first (README: "The replication log").

CREATE TABLE IF NOT EXISTS replication_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- replay order: oldest first
    target       smallint NOT NULL CHECK (target IN (0, 1, 2)),    -- the server that must apply this
    operation    varchar  NOT NULL CHECK (operation IN ('insert', 'update', 'delete')),
    stop_rdt_id  bigint   NOT NULL,   -- which row. Not a foreign key: deleted rows must still be logged
    service_type varchar,             -- read-only on edit, so one copy is enough; also says which fragment

    -- Values before (init_) and after (fin_) the change. Which ones are used depends on operation:
    --   insert: fin_ only  (init_ stays NULL: there was no row before)
    --   update: both       (the replay applies fin_ only if the target still holds init_)
    --   delete: init_ only (fin_ stays NULL: there is no row after)
    -- Real rows can have NULLs too (a first stop has no arrival), which is why operation is stored
    -- explicitly instead of being guessed from empty columns.
    init_service_company    varchar,
    fin_service_company     varchar,
    init_stop_arrival_time  timestamptz,
    fin_stop_arrival_time   timestamptz,
    init_stop_arrival_delay bigint,
    fin_stop_arrival_delay  bigint,

    -- pending:  not delivered yet
    -- applied:  delivered to the target
    -- conflict: the target held neither the init_ nor the fin_ values, so it was left untouched
    status     varchar     NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'conflict')),
    created_at timestamptz NOT NULL DEFAULT now(),  -- when the write happened
    done_at    timestamptz                          -- when it was applied (or found in conflict)
);

-- The replay keeps asking "what's pending for server N, oldest first?". This index answers it
-- instantly by only covering pending entries.
CREATE INDEX IF NOT EXISTS replication_log_pending ON replication_log (target, id) WHERE status = 'pending';

-- Let the "app" login read and write the log (skipped if "app" doesn't exist yet).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON replication_log TO app;
    END IF;
END
$$;
