#!/usr/bin/env bash
# Fills this server's fragment by copying the matching rows from server 0 (the central node).
# Creates the database and the table (from schema/services.sql) if they don't exist yet.
#
# Usage (on server 1 or server 2, as your regular user; it calls sudo itself):
#   bash scripts/load-fragment.sh sprinter            server 1: Sprinter rows only
#   bash scripts/load-fragment.sh others              server 2: every other type
#   bash scripts/load-fragment.sh <fragment> --reset  empty the table first, then copy again
#
# Needs: the network setup done on all servers, and the "app" login allowed to read
# the services table on server 0.

set -euo pipefail

DB_NAME="railway"
TABLE="services"
CENTRAL_HOST="10.20.200.160"   # server 0
APP_USER="app"
# Not named COLUMNS: bash uses that name for the terminal width and overwrites it.
FIELDS="stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_FILE="$REPO_DIR/schema/services.sql"

# The fragmentation rule. IS DISTINCT FROM also catches rows with an empty type,
# so every row of server 0 lands in exactly one fragment.
case "${1:-}" in
    sprinter) WHERE="service_type = 'Sprinter'" ;;
    others)   WHERE="service_type IS DISTINCT FROM 'Sprinter'" ;;
    *)
        echo "Usage: bash scripts/load-fragment.sh sprinter|others [--reset]" >&2
        exit 1
        ;;
esac

RESET=false
if [ "${2:-}" = "--reset" ]; then
    RESET=true
fi

# Local database, as the "postgres" admin. The "cd /" avoids a harmless "could not change directory" warning.
pg() {
    (cd / && sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@")
}

# Server 0's database, over the network, as "app".
central() {
    psql -X -q -v ON_ERROR_STOP=1 -h "$CENTRAL_HOST" -U "$APP_USER" -d "$DB_NAME" "$@"
}

# Ask for both passwords up front, so the two prompts don't collide mid-copy.
echo "==> Checking sudo access"
sudo -v
read -r -s -p "Password for '$APP_USER' on server 0: " PGPASSWORD
echo
export PGPASSWORD   # only lives while this script runs; never saved

echo "==> Checking server 0 ($CENTRAL_HOST) is up"
if ! pg_isready -q -h "$CENTRAL_HOST" -p 5432; then
    echo "Server 0 isn't accepting connections. Is PostgreSQL running there?" >&2
    exit 1
fi

SOURCE_ROWS="$(central -tAc "SELECT count(*) FROM $TABLE WHERE $WHERE")"
echo "    Server 0 has $SOURCE_ROWS rows for the '$1' fragment"

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

if [ "$RESET" = true ]; then
    echo "==> --reset given: emptying table '$TABLE'"
    pg -d "$DB_NAME" -c "TRUNCATE $TABLE;"
fi

LOCAL_ROWS="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"
if [ "$LOCAL_ROWS" != "0" ]; then
    echo "Table '$TABLE' here already has $LOCAL_ROWS rows." >&2
    echo "To empty it and copy again:  bash scripts/load-fragment.sh $1 --reset" >&2
    exit 1
fi

echo "==> Copying rows from server 0 (this takes a few minutes)"
SECONDS=0
# Server 0 streams out only this fragment's rows; the local server streams them in.
if ! (central -c "\copy (SELECT $FIELDS FROM $TABLE WHERE $WHERE) TO STDOUT" \
      | pg -d "$DB_NAME" -c "\copy $TABLE ($FIELDS) FROM STDIN"); then
    echo "The copy failed. Emptying the table so no half-copied fragment is left behind." >&2
    pg -d "$DB_NAME" -c "TRUNCATE $TABLE;"
    exit 1
fi
echo "    Took $((SECONDS / 60)) min $((SECONDS % 60)) s"

LOCAL_ROWS="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"
if [ "$LOCAL_ROWS" != "$SOURCE_ROWS" ]; then
    echo "Row counts don't match: server 0 has $SOURCE_ROWS, this server got $LOCAL_ROWS." >&2
    echo "Emptying the table. Run the script again." >&2
    pg -d "$DB_NAME" -c "TRUNCATE $TABLE;"
    exit 1
fi

echo "==> Refreshing table statistics"
pg -d "$DB_NAME" -c "ANALYZE $TABLE;"

echo "==> Done. Server 0: $SOURCE_ROWS rows, this server: $LOCAL_ROWS rows. They match."
pg -d "$DB_NAME" -c "
SELECT service_type, count(*)
FROM $TABLE GROUP BY 1 ORDER BY 2 DESC;"
