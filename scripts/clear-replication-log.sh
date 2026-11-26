#!/usr/bin/env bash
# Empties this server's replication log (TRUNCATE) and restarts its entry numbers at 1.
# The table itself is kept. Meant for resetting between demo runs.
#
# Careful: a PENDING entry is a write another server hasn't received yet. Deleting it means that
# server never gets the write, and the two copies of that row stay different. The script warns you.
#
# Usage (on the server, as your regular user; it calls sudo itself):
#   bash scripts/clear-replication-log.sh          asks you to confirm first
#   bash scripts/clear-replication-log.sh --yes    skips the confirmation

set -euo pipefail

DB_NAME="railway"
TABLE="replication_log"

pg() {
    (cd / && sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@")
}

if ! pg -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 ||
   [ "$(pg -d "$DB_NAME" -tAc "SELECT to_regclass('public.$TABLE') IS NOT NULL")" != "t" ]; then
    echo "==> Table '$TABLE' doesn't exist on this server: nothing to empty."
    echo "    Create it with:  sudo -u postgres psql -d $DB_NAME < schema/replication_log.sql"
    exit 0
fi

# How many entries there are, per status (e.g. "pending 3, applied 12").
SUMMARY="$(pg -d "$DB_NAME" -tAc "SELECT coalesce(string_agg(status || ' ' || n, ', ' ORDER BY status), 'none')
                                  FROM (SELECT status, count(*) AS n FROM $TABLE GROUP BY status) AS counts")"
PENDING="$(pg -d "$DB_NAME" -tAc "SELECT count(*) FROM $TABLE WHERE status = 'pending'")"

echo "Entries in '$TABLE' on this server: $SUMMARY"
if [ "$PENDING" -gt 0 ]; then
    echo "WARNING: $PENDING entries are still pending. Their target servers will never receive those writes,"
    echo "         so those rows will differ between servers. Let the replay finish first if that matters."
fi

if [ "${1:-}" != "--yes" ]; then
    read -r -p "Type yes to delete all entries: " ANSWER
    if [ "$ANSWER" != "yes" ]; then
        echo "Cancelled. Nothing was changed."
        exit 1
    fi
fi

pg -d "$DB_NAME" -c "TRUNCATE $TABLE RESTART IDENTITY;"
echo "==> '$TABLE' is empty. New entries start again at id 1."
