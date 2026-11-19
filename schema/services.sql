CREATE TABLE services (
    service_RDT_ID               bigint,
    service_DATE                 date,
    service_Type                 varchar,
    service_Company              varchar,
    service_Train_number         bigint,
    service_Completely_cancelled boolean,
    service_Partly_cancelled     boolean,
    service_Maximum_delay        bigint,
    stop_RDT_ID                  bigint,
    stop_Station_code            varchar,
    stop_Station_name            varchar,
    stop_Arrival_time            timestamptz,
    stop_Arrival_delay           bigint,
    stop_Arrival_cancelled       boolean,
    stop_Departure_time          timestamptz,
    stop_Departure_delay         bigint,
    stop_Departure_cancelled     boolean
);
