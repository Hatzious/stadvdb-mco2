-- Temporary landing table with all 17 columns of the CSV, in the CSV's order.
-- scripts/load-data.sh loads the CSV into this first, then copies the 5 kept columns into "services".
-- It is a TEMP table: PostgreSQL deletes it automatically when the load finishes.
CREATE TEMP TABLE incoming (
    service_rdt_id               bigint,
    service_date                 date,
    service_type                 varchar,
    service_company              varchar,
    service_train_number         bigint,
    service_completely_cancelled boolean,
    service_partly_cancelled     boolean,
    service_maximum_delay        bigint,
    stop_rdt_id                  bigint,
    stop_station_code            varchar,
    stop_station_name            varchar,
    stop_arrival_time            timestamptz,
    stop_arrival_delay           bigint,
    stop_arrival_cancelled       boolean,
    stop_departure_time          timestamptz,
    stop_departure_delay         bigint,
    stop_departure_cancelled     boolean
);
