"""Access layer, writes: insert, update and delete a row on both of its copies.

Every row has two copies: one on server 0, one on its fragment (Sprinter -> server 1, others -> server 2).
Each write:

    1. picks the primary: server 0 if usable, otherwise the fragment (the other copy is the secondary)
    2. runs ONE transaction on the primary, at the isolation level the caller chose:
           the data change, with a before-check (update/delete only apply if the row still holds the
           values the user saw; otherwise EditConflict), then a replication_log entry for the secondary
    3. delivers right away (replication.deliver), so normally both copies match before the user is told
    4. returns a WriteResult saying where it committed and whether the other copy is up to date

If the primary is down, the write moves to the other copy and its log entry waits for the primary.
If the primary refuses (conflict, lock timeout, serialization failure), nothing is written anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import db
import replication
from access import CENTRAL, OTHERS_SERVER, SPRINTER_SERVER, Service, Unavailable, fragment_for, mark_down, mark_up, usable
from settings import Settings

INSERT_ATTEMPTS = 3  # retries when someone else took the same new ID a moment earlier


class EditConflict(db.Refused):
    """The row no longer holds the values the user saw: someone changed or deleted it in the meantime."""


@dataclass(frozen=True)
class WriteResult:
    operation: str  # "insert", "update" or "delete"
    stop_rdt_id: int
    primary: int  # where the write committed first
    secondary: int  # the other copy
    entry_id: int  # the log entry on the primary, meant for the secondary
    delivery: replication.DeliveryResult

    @property
    def other_copy(self) -> str:
        """ "applied" (both copies match), "pending" (waiting for the replay) or "conflict"."""
        return self.delivery.status_of(self.entry_id)


def _commit_on_primary(
    settings: Settings,
    primary: int,
    secondary: int,
    operation: str,
    before: Service | None,
    after: Service | None,
    isolation: str,
    on_event: db.EventHandler | None,
) -> int:
    """The data change plus its log entry, in one transaction. Returns the log entry's ID."""
    row = after or before
    with db.transaction(settings, primary, isolation, on_event) as cur:
        # 1. The data change first: it takes the row lock, so two writers to one row queue up here and
        #    the second one gets its log entry ID only after the first has committed.
        if operation == "update":
            cur.execute(
                "UPDATE services SET service_company = %s, stop_arrival_time = %s, stop_arrival_delay = %s"
                f" WHERE {replication.SAME_VALUES}",
                (after.service_company, after.stop_arrival_time, after.stop_arrival_delay,
                 *replication.same_values_params(before)),
            )
        elif operation == "delete":
            cur.execute(f"DELETE FROM services WHERE {replication.SAME_VALUES}", replication.same_values_params(before))
        else:  # insert; a taken ID fails with "duplicate key", which arrives as Refused
            cur.execute(
                "INSERT INTO services (stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay)"
                " VALUES (%s, %s, %s, %s, %s)",
                replication.same_values_params(after),
            )
        if cur.rowcount != 1:
            raise EditConflict(primary, f"row {row.stop_rdt_id} changed or was deleted since it was read")

        # 2. The note for the other copy, in the same transaction: both commit, or neither does.
        cur.execute(replication.INSERT_LOG_ENTRY, replication.log_entry_values(secondary, operation, before, after))
        entry_id = cur.fetchone()[0]
        db.emit(on_event, primary, "logged", f"entry #{entry_id} for server {secondary}")
    return entry_id


def _write(
    settings: Settings,
    operation: str,
    before: Service | None,
    after: Service | None,
    isolation: str,
    on_event: db.EventHandler | None,
) -> WriteResult:
    row = after or before
    fragment = fragment_for(row.service_type)

    unavailable: list[int] = []
    for primary, secondary in ((CENTRAL, fragment), (fragment, CENTRAL)):
        if not usable(settings, primary):
            unavailable.append(primary)
            continue
        try:
            entry_id = _commit_on_primary(settings, primary, secondary, operation, before, after, isolation, on_event)
        except db.ServerDown:
            # If it died during COMMIT, the write may have landed anyway. Writing it on the other copy is
            # still safe: each copy's log entry will find the other copy already holding the result.
            mark_down(primary)
            unavailable.append(primary)
            continue
        mark_up(primary)
        delivery = replication.deliver(settings, source=primary, target=secondary, on_event=on_event)
        return WriteResult(operation, row.stop_rdt_id, primary, secondary, entry_id, delivery)

    names = " and ".join(f"server {s}" for s in unavailable)
    raise Unavailable(f"Can't save row {row.stop_rdt_id} right now: {names} unavailable.")


def update_row(
    settings: Settings,
    before: Service,
    after: Service,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> WriteResult:
    """Change a row. `before` is the row as the user saw it; `after` is what it should become.

    Raises EditConflict if the row no longer matches `before`.
    """
    if after.stop_rdt_id != before.stop_rdt_id:
        raise ValueError("The ID can't be changed.")
    if after.service_type != before.service_type:
        raise ValueError("The service type can't be changed: delete the row and insert it again instead.")
    return _write(settings, "update", before, after, isolation, on_event)


def delete_row(
    settings: Settings,
    before: Service,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> WriteResult:
    """Delete a row, if it still holds the values the user saw (`before`). Otherwise EditConflict."""
    return _write(settings, "delete", before, None, isolation, on_event)


def next_id(settings: Settings, on_event: db.EventHandler | None = None) -> int:
    """The ID for a new row: one more than the highest existing ID.

    Asks server 0, or both fragments while server 0 is down (each only knows its own rows).
    """
    sources = [(CENTRAL,), (SPRINTER_SERVER, OTHERS_SERVER)]
    for group in sources:
        if not all(usable(settings, s) for s in group):
            continue
        try:
            highest = 0
            for server_id in group:
                with db.transaction(settings, server_id, on_event=on_event) as cur:
                    cur.execute("SELECT coalesce(max(stop_rdt_id), 0) FROM services")  # instant: primary key index
                    highest = max(highest, cur.fetchone()[0])
                mark_up(server_id)
            return highest + 1
        except db.ServerDown as error:
            mark_down(error.server_id)
    raise Unavailable("Can't pick a new ID right now: not enough servers are reachable.")


def insert_row(
    settings: Settings,
    service_type: str | None,
    service_company: str | None,
    stop_arrival_time: datetime | None,
    stop_arrival_delay: int | None,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> WriteResult:
    """Add a row. The app picks its ID; if someone took that ID a moment earlier, it tries the next one."""
    for attempt in range(1, INSERT_ATTEMPTS + 1):
        row = Service(next_id(settings, on_event), service_type, service_company, stop_arrival_time, stop_arrival_delay)
        try:
            return _write(settings, "insert", None, row, isolation, on_event)
        except db.Refused as error:
            if error.sqlstate != "23505" or attempt == INSERT_ATTEMPTS:  # 23505: duplicate key
                raise
    raise AssertionError("unreachable")
