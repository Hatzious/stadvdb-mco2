-- The table every node stores. Node 1 (Hera) holds all rows; Nodes 2 and 3 each hold one fragment.
-- Only 5 of the CSV's 17 columns are kept (see schema/incoming.sql for the full CSV layout).
CREATE TABLE services (
    stop_rdt_id         bigint PRIMARY KEY,  -- unique per row, never empty (checked on the 2021 data)
    service_type        varchar,             -- fragmentation option: 'Sprinter' (~45%) vs. the rest
    service_company     varchar,             -- fragmentation option: NS (~67%) vs. the rest
    stop_arrival_time   timestamptz,
    stop_arrival_delay  bigint               -- minutes late vs. the scheduled arrival
);
