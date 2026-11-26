"""Delivery: copies the writes recorded in one server's replication_log to the server they're meant for.

    deliver(settings, source=0, target=1)

runs one transaction on the source server (the one holding the log):

    1. take an advisory lock named after the target, so only one delivery to that target runs at a time
       (others wait their turn, even from another server's copy of the app)
    2. read the pending entries for that target, oldest first
    3. apply each one on the target, in a small transaction of its own, checking what the target holds:
           the init_ values (or no row, for an insert)   -> apply the change     -> "applied"
           the fin_ values already (or no row, delete)  -> nothing to do         -> "applied"
           anything else                                -> leave it untouched    -> "conflict"
    4. mark each entry, then commit (which also releases the lock)

If the target goes down midway, delivery stops at that entry; the rest stay pending for the next try.
If the source fails before committing, the marks are lost and the entries are delivered again later;
the "already has the fin_ values" rule makes that harmless.

Delivery always runs at READ COMMITTED, whatever isolation level the user picked: it is the system's
own background work, not one of the transactions being studied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from psycopg.rows import class_row

import db
from access import Service, forget_readiness, mark_down, mark_up, recently_down
from settings import Settings

# How often the replay service (app/replay_worker.py) delivers each server's log.
# Changing it: edit, commit, then on each server git pull + sudo bash scripts/deploy-app.sh.
REPLAY_INTERVAL_SECONDS = 40

DELIVERY_ISOLATION = "READ COMMITTED"
# Advisory locks are named by two numbers. The first marks "replication delivery" (so no other use of
# advisory locks can clash with it); the second is the target server.
_LOCK_NAMESPACE = 4201


@dataclass(frozen=True)
class LogEntry:
    """One row of replication_log (schema/replication_log.sql)."""

    id: int
    target: int
    operation: str  # "insert", "update" or "delete"
    stop_rdt_id: int
    service_type: str | None
    init_service_company: str | None
    fin_service_company: str | None
    init_stop_arrival_time: datetime | None
    fin_stop_arrival_time: datetime | None
    init_stop_arrival_delay: int | None
    fin_stop_arrival_delay: int | None
    status: str  # "pending", "applied" or "conflict"
    created_at: datetime
    done_at: datetime | None

    @property
    def init(self) -> Service | None:
        """The row before the change (None for an insert)."""
        if self.operation == "insert":
            return None
        return Service(
            self.stop_rdt_id,
            self.service_type,
            self.init_service_company,
            self.init_stop_arrival_time,
            self.init_stop_arrival_delay,
        )

    @property
    def fin(self) -> Service | None:
        """The row after the change (None for a delete)."""
        if self.operation == "delete":
            return None
        return Service(
            self.stop_rdt_id,
            self.service_type,
            self.fin_service_company,
            self.fin_stop_arrival_time,
            self.fin_stop_arrival_delay,
        )


@dataclass
class DeliveryResult:
    source: int
    target: int
    applied: list[int] = field(default_factory=list)  # entry IDs delivered (or found already there)
    conflicts: list[int] = field(default_factory=list)  # entry IDs the target disagreed with
    stopped: str | None = None  # why delivery stopped early, if it did; the rest stay pending
    unreachable: int | None = None  # the server that couldn't be reached, if that was the reason

    def status_of(self, entry_id: int) -> str:
        if entry_id in self.applied:
            return "applied"
        if entry_id in self.conflicts:
            return "conflict"
        return "pending"


# The columns a log entry stores, in the order the SQL below lists them.
_LOG_COLUMNS = (
    "target, operation, stop_rdt_id, service_type, "
    "init_service_company, fin_service_company, "
    "init_stop_arrival_time, fin_stop_arrival_time, "
    "init_stop_arrival_delay, fin_stop_arrival_delay"
)


def log_entry_values(target: int, operation: str, init: Service | None, fin: Service | None) -> tuple:
    """The values for one INSERT INTO replication_log, matching _LOG_COLUMNS."""
    row = fin or init
    assert row is not None
    return (
        target,
        operation,
        row.stop_rdt_id,
        row.service_type,
        init.service_company if init else None,
        fin.service_company if fin else None,
        init.stop_arrival_time if init else None,
        fin.stop_arrival_time if fin else None,
        init.stop_arrival_delay if init else None,
        fin.stop_arrival_delay if fin else None,
    )


INSERT_LOG_ENTRY = f"INSERT INTO replication_log ({_LOG_COLUMNS}) VALUES ({', '.join(['%s'] * 10)}) RETURNING id"

# "The row still holds exactly these values". IS NOT DISTINCT FROM treats two NULLs as equal, unlike =.
SAME_VALUES = (
    "stop_rdt_id = %s"
    " AND service_type IS NOT DISTINCT FROM %s"
    " AND service_company IS NOT DISTINCT FROM %s"
    " AND stop_arrival_time IS NOT DISTINCT FROM %s"
    " AND stop_arrival_delay IS NOT DISTINCT FROM %s"
)


def same_values_params(row: Service) -> tuple:
    return (row.stop_rdt_id, row.service_type, row.service_company, row.stop_arrival_time, row.stop_arrival_delay)


def _current(cur, stop_rdt_id: int) -> Service | None:
    cur.execute(
        "SELECT stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay"
        " FROM services WHERE stop_rdt_id = %s",
        (stop_rdt_id,),
    )
    found = cur.fetchone()
    return Service(*found) if found else None


def _apply_entry(settings: Settings, entry: LogEntry, on_event: db.EventHandler | None) -> str:
    """Apply one log entry on its target. Returns "applied" or "conflict"."""
    init, fin = entry.init, entry.fin
    with db.transaction(settings, entry.target, DELIVERY_ISOLATION, on_event) as cur:
        if entry.operation == "update":
            cur.execute(
                "UPDATE services SET service_company = %s, stop_arrival_time = %s, stop_arrival_delay = %s"
                f" WHERE {SAME_VALUES}",
                (fin.service_company, fin.stop_arrival_time, fin.stop_arrival_delay, *same_values_params(init)),
            )
        elif entry.operation == "insert":
            cur.execute(
                "INSERT INTO services (stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (stop_rdt_id) DO NOTHING",
                same_values_params(fin),
            )
        else:  # delete
            cur.execute(f"DELETE FROM services WHERE {SAME_VALUES}", same_values_params(init))

        if cur.rowcount == 1:
            return "applied"
        # Nothing changed: either the target already has the result, or it holds something unexpected.
        return "applied" if _current(cur, entry.stop_rdt_id) == fin else "conflict"


def deliver(settings: Settings, source: int, target: int, on_event: db.EventHandler | None = None) -> DeliveryResult:
    """Deliver every pending entry in `source`'s log that is meant for `target`, oldest first."""
    result = DeliveryResult(source, target)
    if recently_down(target):
        result.stopped = f"server {target} was unreachable moments ago"
        result.unreachable = target
        return result

    try:
        with db.transaction(settings, source, DELIVERY_ISOLATION, on_event) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NAMESPACE, target))  # waits its turn
            cur.row_factory = class_row(LogEntry)
            cur.execute(
                "SELECT * FROM replication_log WHERE target = %s AND status = 'pending' ORDER BY id",
                (target,),
            )
            entries = cur.fetchall()
            db.emit(on_event, source, "delivering", f"{len(entries)} pending for server {target}")

            for entry in entries:
                try:
                    outcome = _apply_entry(settings, entry, on_event)
                except db.ServerDown as error:
                    mark_down(target)
                    result.stopped = f"server {target} unreachable: {error.reason}"
                    result.unreachable = target
                    break
                except db.Refused as error:  # e.g. a lock on the target row didn't free up in time
                    result.stopped = f"server {target} refused entry #{entry.id}: {error.reason}"
                    break
                mark_up(target)
                cur.execute("UPDATE replication_log SET status = %s, done_at = now() WHERE id = %s", (outcome, entry.id))
                (result.applied if outcome == "applied" else result.conflicts).append(entry.id)
                db.emit(on_event, target, outcome, f"entry #{entry.id} ({entry.operation} {entry.stop_rdt_id})")
    except (db.ServerDown, db.Refused) as error:
        # The source failed before its commit, so none of the marks above were saved: those entries
        # stay pending and are delivered again later (harmlessly, thanks to the fin_ check).
        down = isinstance(error, db.ServerDown)
        if down:
            mark_down(source)
        return DeliveryResult(source, target, stopped=f"server {source} (the log) failed: {error.reason}",
                              unreachable=source if down else None)
    if result.stopped is None:
        forget_readiness(target)  # it may have just caught up: let the next check ask again
    return result


def replay_all(settings: Settings, on_event: db.EventHandler | None = None) -> list[DeliveryResult]:
    """Deliver every log to every other server (the "Replay now" button). Skips servers known to be down."""
    results = []
    for source in sorted(settings.servers):
        for target in sorted(settings.servers):
            if source == target:
                continue
            if recently_down(source):
                results.append(DeliveryResult(source, target, stopped=f"server {source} (the log) is unreachable",
                                              unreachable=source))
                continue
            results.append(deliver(settings, source, target, on_event))
    return results


@dataclass(frozen=True)
class LogSummary:
    """What one server's log holds for one target."""

    source: int  # the server holding the log
    target: int
    pending: int
    conflicts: int
    oldest_pending: datetime | None


def log_summaries(settings: Settings) -> tuple[list[LogSummary], list[int]]:
    """Pending and conflicting entries in every reachable log. Also returns the logs that couldn't be read."""
    summaries, unreachable = [], []
    for source in sorted(settings.servers):
        if recently_down(source):
            unreachable.append(source)
            continue
        try:
            with db.transaction(settings, source) as cur:
                cur.execute(
                    "SELECT target,"
                    " count(*) FILTER (WHERE status = 'pending'),"
                    " count(*) FILTER (WHERE status = 'conflict'),"
                    " min(created_at) FILTER (WHERE status = 'pending')"
                    " FROM replication_log GROUP BY target ORDER BY target"
                )
                rows = cur.fetchall()
        except db.ServerDown:
            mark_down(source)
            unreachable.append(source)
            continue
        summaries += [LogSummary(source, *row) for row in rows]
    return summaries, unreachable


def get_entry(settings: Settings, server_id: int, entry_id: int) -> LogEntry | None:
    """Read one entry from one server's log (for checks and displays)."""
    with db.transaction(settings, server_id) as cur:
        cur.row_factory = class_row(LogEntry)
        cur.execute("SELECT * FROM replication_log WHERE id = %s", (entry_id,))
        return cur.fetchone()
