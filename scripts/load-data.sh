#!/usr/bin/env bash
# Loads the downloaded dataset from data/ into the PostgreSQL "services" table.
# Creates the database and the table (from schema/services.sql) if they don't exist yet.
#
# - Empty table:               loads every row directly (fastest).
# - Table already has rows:    loads only the rows that aren't there yet, and skips the rest.
#                              This needs a primary key on the table: without one, PostgreSQL
#                              has no way to tell which rows "already exist".
#
# Usage (on the node, as your regular user; it calls sudo itself):
#   bash scripts/load-data.sh
# To wipe the table and load everything again, use scripts/reload-data.sh instead.

set -euo pipefail

DB_NAME="railway"
TABLE="services"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_FILE="$REPO_DIR/schema/services.sql"
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
    echo "==> Database '$DB_NAME' already exists"
else
    echo "==> Creating database '$DB_NAME'"
    (cd / && sudo -u postgres createdb "$DB_NAME")
fi

if [ "$(pg -d "$DB_NAME" -tAc "SELECT to_regclass('public.$TABLE') IS NOT NULL")" = "t" ]; then
    echo "==> Table '$TABLE' already exists"
else
    echo "==> Creating table '$TABLE' from schema/services.sql"
    pg -d "$DB_NAME" < "$SCHEMA_FILE"
fi

ROWS_BEFORE="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"

if [ "$ROWS_BEFORE" = "0" ]; then
    echo "==> Table is empty: loading every row (this takes a few minutes)"
    time (zcat "$FILE" | pg -d "$DB_NAME" -c "\copy $TABLE FROM STDIN WITH (FORMAT csv, HEADER true)")
else
    HAS_KEY="$(pg -d "$DB_NAME" -tAc "
        SELECT count(*) > 0 FROM pg_constraint
        WHERE conrelid = 'public.$TABLE'::regclass AND contype IN ('p', 'u')")"
    if [ "$HAS_KEY" != "t" ]; then
        echo "Table '$TABLE' already has $ROWS_BEFORE rows, but no primary key." >&2
        echo "Without a key, PostgreSQL can't tell which rows already exist, so nothing was loaded." >&2
        echo "Either add a primary key first, or wipe and reload:  bash scripts/reload-data.sh" >&2
        exit 1
    fi

    echo "==> Table already has $ROWS_BEFORE rows: loading only the missing ones (this takes a few minutes)"
    echo "    Rows go into a temporary copy first, so this needs extra free disk space for a while."
    # All steps run as one transaction (-1): if anything fails, nothing is added.
    # The temporary table disappears automatically when psql exits.
    time (zcat "$FILE" | pg -1 -d "$DB_NAME" \
        -c "CREATE TEMP TABLE incoming (LIKE $TABLE INCLUDING DEFAULTS)" \
        -c "\copy incoming FROM pstdin WITH (FORMAT csv, HEADER true)" \
        -c "INSERT INTO $TABLE SELECT * FROM incoming ON CONFLICT DO NOTHING")
fi

ROWS_AFTER="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"
echo "==> Rows before: $ROWS_BEFORE   rows after: $ROWS_AFTER   added: $((ROWS_AFTER - ROWS_BEFORE))"

echo "==> Refreshing table statistics"
pg -d "$DB_NAME" -c "ANALYZE $TABLE;"

echo "==> Done. Summary:"
pg -d "$DB_NAME" -c "
SELECT count(*)                                         AS rows,
       min(service_date)                                AS first_date,
       max(service_date)                                AS last_date,
       pg_size_pretty(pg_total_relation_size('$TABLE')) AS table_size
FROM $TABLE;"
