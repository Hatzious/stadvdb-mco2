#!/usr/bin/env bash
# Empties the "services" table (TRUNCATE) and loads the dataset into it again from scratch.
# The table itself is kept, including any primary key or indexes you added; only the rows are deleted.
# If schema/services.sql has changed, this does NOT apply the change: drop the table instead.
#
# Usage (on the node, as your regular user; it calls sudo itself):
#   bash scripts/reload-data.sh          asks you to confirm first
#   bash scripts/reload-data.sh --yes    skips the confirmation

set -euo pipefail

DB_NAME="railway"
TABLE="services"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

pg() {
    (cd / && sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@")
}

TABLE_EXISTS=false
if pg -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 &&
   [ "$(pg -d "$DB_NAME" -tAc "SELECT to_regclass('public.$TABLE') IS NOT NULL")" = "t" ]; then
    TABLE_EXISTS=true
fi

if [ "$TABLE_EXISTS" = true ]; then
    ROWS="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE")"

    if [ "${1:-}" != "--yes" ]; then
        echo "This permanently deletes all $ROWS rows in '$TABLE' (database '$DB_NAME') and loads them again."
        read -r -p "Type yes to continue: " ANSWER
        if [ "$ANSWER" != "yes" ]; then
            echo "Cancelled. Nothing was changed."
            exit 1
        fi
    fi

    echo "==> Emptying table '$TABLE'"
    pg -d "$DB_NAME" -c "TRUNCATE $TABLE;"
else
    echo "==> Table '$TABLE' doesn't exist yet: nothing to empty"
fi

exec bash "$REPO_DIR/scripts/load-data.sh"
