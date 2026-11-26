-- Indexes for sorting the Records page by arrival time or by delay. Every server builds its own
-- (indexes aren't replicated): server 0 on all rows, each fragment on its share.
--
-- Separate from services.sql on purpose: keeping an index up to date while millions of rows are
-- bulk-loaded is slow, so the load scripts run this file AFTER loading.
--
-- Each index covers (column, stop_rdt_id): the ID breaks ties, so paging can continue from an exact
-- "after (delay 5, ID 49227490)". They are partial (WHERE ... IS NOT NULL): rows without a value
-- aren't listed when sorting by that column, so the index doesn't need them, and is smaller.
--
-- CONCURRENTLY builds without blocking reads and writes, so it's safe on a server in use. It can't run
-- inside a transaction, so run this file as is (not with psql -1). Safe to run again: IF NOT EXISTS
-- skips indexes already there. If a build is interrupted it leaves an unusable index behind; drop it
-- (README: "Sort indexes") and run this file again.

CREATE INDEX CONCURRENTLY IF NOT EXISTS services_by_arrival_time
    ON services (stop_arrival_time, stop_rdt_id) WHERE stop_arrival_time IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS services_by_arrival_delay
    ON services (stop_arrival_delay, stop_rdt_id) WHERE stop_arrival_delay IS NOT NULL;
