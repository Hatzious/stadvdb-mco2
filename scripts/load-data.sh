#!/usr/bin/env bash
# Loads the downloaded dataset from data/ into the PostgreSQL "services" table.
# Creates the database and the table (from schema/services.sql) if they don't exist yet.
#
# How it loads, in two steps:
#   1. The whole CSV (all 17 columns) goes into a temporary table, "incoming" (schema/incoming.sql).
#   2. Only the 5 kept columns are copied into "services". Rows whose stop_rdt_id is already
#      there are skipped, so running this twice never duplicates data.
# The temporary table is deleted automatically at the end, but needs extra free disk space while it exists.
#
# Usage (on the node, as your regular user; it calls sudo itself):
#   bash scripts/load-data.sh
# To wipe the table and load everything again, use scripts/reload-data.sh instead.

set -euo pipefail

DB_NAME="railway"
TABLE="services"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_FILE="$REPO_DIR/schema/services.sql"
INCOMING_FILE="$REPO_DIR/schema/incoming.sql"
URL="$(head -n 1 "$REPO_DIR/source.txt" | tr -d '[:space:]')"
FILE="$REPO_DIR/data/$(basename "$URL")"

# Run SQL as the "postgres" database admin. ON_ERROR_STOP makes psql fail loudly instead of carrying on.
# The "cd /" avoids a harmless "could not change directory" warning, since postgres can't enter your home folder.
pg() {
    (cd / && sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@")
}

if [ ! -f "$FILE" ]; then
    echo "Dataset not found at $FILE" >&2
    echo "Run this first:  bash scripts/download-data.sh" >&2
    exit 1
fi

echo "==> Checking the file is a complete gzip archive"
gzip -t "$FILE"

if pg -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1; then
    # An old database may use SQL_ASCII ("no encoding"), which makes the app receive text as raw bytes.
    ENCODING="$(pg -d postgres -tAc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = '$DB_NAME'")"
    if [ "$ENCODING" != "UTF8" ]; then
        echo "Database '$DB_NAME' uses encoding $ENCODING, but the app expects UTF8." >&2
        echo "Drop it (this deletes its data), then run this script again:" >&2
        echo "    sudo -u postgres psql -c \"DROP DATABASE $DB_NAME WITH (FORCE);\"" >&2
        exit 1
    fi
    echo "==> Database '$DB_NAME' already exists (UTF8)"
else
    # Spelled out because the server's default can be SQL_ASCII when the container had no language set.
    # template0 is the blank template, the only one a new database can change its encoding from.
    echo "==> Creating database '$DB_NAME' (UTF8)"
    (cd / && sudo -u postgres createdb --encoding=UTF8 --locale=C.UTF-8 --template=template0 "$DB_NAME")
fi

if [ "$(pg -d "$DB_NAME" -tAc "SELECT to_regclass('public.$TABLE') IS NOT NULL")" = "t" ]; then
    echo "==> Table '$TABLE' already exists"
else
    echo "==> Creating table '$TABLE' from schema/services.sql"
    pg -d "$DB_NAME" < "$SCHEMA_FILE"
fi

# Stop early if the existing table is an older layout (e.g. the first 17-column version).
TABLE_COLUMNS="$(pg -d "$DB_NAME" -tAc "
    SELECT string_agg(column_name, ',' ORDER BY column_name)
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = '$TABLE'")"
EXPECTED="service_company,service_type,stop_arrival_delay,stop_arrival_time,stop_rdt_id"
if [ "$TABLE_COLUMNS" != "$EXPECTED" ]; then
    echo "Table '$TABLE' doesn't match schema/services.sql." >&2
    echo "  It has:   $TABLE_COLUMNS" >&2
    echo "  Expected: $EXPECTED" >&2
    echo "Drop it so this script can recreate it (this deletes its rows):" >&2
    echo "  sudo -u postgres psql -d $DB_NAME -c \"DROP TABLE $TABLE;\"" >&2
    exit 1
fi

HAS_KEY="$(pg -d "$DB_NAME" -tAc "
    SELECT count(*) > 0 FROM pg_constraint
    WHERE conrelid = 'public.$TABLE'::regclass AND contype = 'p'")"
if [ "$HAS_KEY" != "t" ]; then
    echo "Table '$TABLE' has no primary key, so already-loaded rows can't be detected and skipped." >&2
    echo "Drop the table and run this script again to recreate it from schema/services.sql." >&2
    exit 1
fi

ROWS_BEFORE="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"

echo "==> Loading $FILE (this takes several minutes)"
# All steps run as one transaction (-1): if anything fails, nothing is added.
time (zcat "$FILE" | pg -1 -d "$DB_NAME" \
    -c "$(cat "$INCOMING_FILE")" \
    -c "\copy incoming FROM pstdin WITH (FORMAT csv, HEADER true)" \
    -c "INSERT INTO $TABLE (stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay)
        SELECT stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay
        FROM incoming
        ON CONFLICT (stop_rdt_id) DO NOTHING")

ROWS_AFTER="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"
echo "==> Rows before: $ROWS_BEFORE   rows after: $ROWS_AFTER   added: $((ROWS_AFTER - ROWS_BEFORE))"

echo "==> Refreshing table statistics"
pg -d "$DB_NAME" -c "ANALYZE $TABLE;"

echo "==> Done. Summary:"
pg -d "$DB_NAME" -c "
SELECT count(*)                                         AS rows,
       count(*) FILTER (WHERE service_type = 'Sprinter') AS sprinter_rows,
       pg_size_pretty(pg_total_relation_size('$TABLE')) AS table_size
FROM $TABLE;"
