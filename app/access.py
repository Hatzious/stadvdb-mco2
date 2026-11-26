"""Access layer, reads: decides which server answers, so the pages never have to.

    get_row(settings, stop_rdt_id)      one row: this server first, then server 0, then the other fragment
    list_rows(settings, filters, sort)  a page of 20 by ID, arrival time or delay, with Next / Previous
    jump_to_page(settings, filters, n)  a typed page number (needs one server holding every matching row)

Every result says which server(s) answered. The Records page ignores that (data transparency);
the Simulation and the Node monitor show it.

Where rows live: server 0 holds all of them; server 1 holds service_type = 'Sprinter';
server 2 holds every other type. A list is complete if it comes from server 0, or from the
fragment owning a type filter, or from servers 1 and 2 together (merged in stop_rdt_id order).
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import class_row

import db
from settings import Settings

PAGE_SIZE = 20

CENTRAL = 0  # server 0: every row
SPRINTER_SERVER = 1  # server 1: service_type = 'Sprinter'
OTHERS_SERVER = 2  # server 2: every other type, including an empty one
SPRINTER = "Sprinter"

# After a server fails to answer, skip it for this many seconds instead of waiting for its connection
# timeout on every click. Shared by everyone using this copy of the app.
RECENTLY_DOWN_SECONDS = 10

_COLUMNS = "stop_rdt_id, service_type, service_company, stop_arrival_time, stop_arrival_delay"


@dataclass(frozen=True)
class Service:
    """One row of the services table."""

    stop_rdt_id: int
    service_type: str | None
    service_company: str | None
    stop_arrival_time: datetime | None
    stop_arrival_delay: int | None


@dataclass(frozen=True)
class Filters:
    """What the user searched for. None means "don't filter on this"."""

    stop_rdt_id: int | None = None
    service_type: str | None = None  # exact match, so the owning fragment is always known
    service_company: str | None = None  # ignores upper/lower case: "ns" also finds "NS"


ID_COLUMN = "stop_rdt_id"
SORT_COLUMNS = (ID_COLUMN, "stop_arrival_time", "stop_arrival_delay")


@dataclass(frozen=True)
class Sort:
    """The order rows are listed in. Sorting by time or delay uses the indexes in
    schema/services_indexes.sql and leaves out rows without a value in that column."""

    column: str = ID_COLUMN  # one of SORT_COLUMNS
    descending: bool = False

    def __post_init__(self) -> None:
        if self.column not in SORT_COLUMNS:  # the name goes into the SQL text, so only allow known ones
            raise ValueError(f"Can't sort by {self.column!r}. Use one of {SORT_COLUMNS}.")

    def key(self, row: Service) -> tuple:
        """Where a row sits in this order: (value, ID), or just (ID,) when sorting by ID."""
        if self.column == ID_COLUMN:
            return (row.stop_rdt_id,)
        return (getattr(row, self.column), row.stop_rdt_id)


@dataclass(frozen=True)
class RowResult:
    row: Service | None  # None: no server that answered has this row, so it doesn't exist
    served_by: tuple[int, ...]  # the server that had it, or every server that said "not here"


@dataclass(frozen=True)
class Page:
    rows: list[Service]  # in stop_rdt_id order, at most PAGE_SIZE
    has_previous: bool
    has_next: bool
    served_by: tuple[int, ...]  # (0,) normally; (1, 2) when the fragments were merged


class Unavailable(Exception):
    """No reachable server (or pair of fragments) can give a complete answer right now."""


def fragment_for(service_type: str | None) -> int:
    """Which fragment server holds rows of this type (same rule scripts/load-fragment.sh used)."""
    return SPRINTER_SERVER if service_type == SPRINTER else OTHERS_SERVER


# --- Which servers are usable -----------------------------------------------------------------

_down_until: dict[int, float] = {}
_down_lock = threading.Lock()  # reads can run in several threads at once


def mark_down(server_id: int) -> None:
    with _down_lock:
        _down_until[server_id] = time.monotonic() + RECENTLY_DOWN_SECONDS


def mark_up(server_id: int) -> None:
    with _down_lock:
        _down_until.pop(server_id, None)


def recently_down(server_id: int) -> bool:
    with _down_lock:
        return time.monotonic() < _down_until.get(server_id, 0.0)


# A server is "recovering" while another server's log holds writes it missed. Entries younger than this
# don't count: in normal operation every entry is pending for a moment between its commit and its
# delivery, and that moment mustn't make a healthy server look like it's recovering.
MISSED_AFTER_SECONDS = 5
# How long a readiness answer is reused before asking the logs again (saves two queries per click).
READY_CACHE_SECONDS = 5

_ready_until: dict[int, tuple[float, bool]] = {}  # server -> (valid until, ready?)
_ready_lock = threading.Lock()


def _missed_writes_in_log(settings: Settings, holder: int, server_id: int) -> bool:
    """Whether `holder`'s log has entries for `server_id` that should have been delivered by now."""
    with db.transaction(settings, holder) as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM replication_log WHERE target = %s AND status = 'pending'"
            f" AND created_at < now() - interval '{MISSED_AFTER_SECONDS} seconds')",
            (server_id,),
        )
        return cur.fetchone()[0]


def is_ready(settings: Settings, server_id: int) -> bool:
    """False while a server is recovering: another server's log still holds writes it missed.

    A recovering server is skipped for reads and writes, like a down one, until the replay has caught
    it up. A log that can't be reached can't be checked; it's ignored (one failure at a time).
    """
    now = time.monotonic()
    with _ready_lock:
        cached = _ready_until.get(server_id)
        if cached and now < cached[0]:
            return cached[1]

    ready = True
    for holder in settings.servers:
        if holder == server_id or recently_down(holder):
            continue
        try:
            if _missed_writes_in_log(settings, holder, server_id):
                ready = False
                break
        except db.ServerDown:
            mark_down(holder)

    with _ready_lock:
        _ready_until[server_id] = (time.monotonic() + READY_CACHE_SECONDS, ready)
    return ready


def forget_readiness(server_id: int) -> None:
    """Drop the cached answer, e.g. right after a replay caught the server up."""
    with _ready_lock:
        _ready_until.pop(server_id, None)


def usable(settings: Settings, server_id: int) -> bool:
    return not recently_down(server_id) and is_ready(settings, server_id)


# --- Talking to one server ------------------------------------------------------------------------


def _select_page(
    settings: Settings,
    server_id: int,
    filters: Filters,
    sort: Sort,
    after: tuple | None,
    before: tuple | None,
    offset: int,
    limit: int,
    isolation: str,
    on_event: db.EventHandler | None,
) -> list[Service]:
    """Run one SELECT on one server.

    Rows come back in `sort` order, or in reverse when `before` is set (paging backwards).
    `after` / `before` are sort keys (see Sort.key) of the row the page continues from.
    """
    clauses: list[str] = []
    params: list[object] = []
    if filters.stop_rdt_id is not None:
        clauses.append("stop_rdt_id = %s")
        params.append(filters.stop_rdt_id)
    if filters.service_type is not None:
        clauses.append("service_type = %s")
        params.append(filters.service_type)
    if filters.service_company is not None:
        clauses.append("lower(service_company) = lower(%s)")
        params.append(filters.service_company)
    if sort.column != ID_COLUMN:
        # Sorted views only list rows that have a value (and that's what the partial index holds).
        clauses.append(f"{sort.column} IS NOT NULL")

    backwards = before is not None
    descending = sort.descending != backwards  # paging backwards flips the direction
    key = after if after is not None else before
    if key is not None:
        # Keyset paging: continue right after (or before) the row we left off at, in sort order.
        # (delay, id) > (5, 49227490) compares delay first and uses the id only to break ties.
        compare = "<" if descending else ">"
        if sort.column == ID_COLUMN:
            clauses.append(f"stop_rdt_id {compare} %s")
        else:
            clauses.append(f"({sort.column}, stop_rdt_id) {compare} (%s, %s)")
        params += list(key)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    direction = "DESC" if descending else "ASC"
    order = f"{sort.column} {direction}" + ("" if sort.column == ID_COLUMN else f", stop_rdt_id {direction}")
    # Only fixed words go into the SQL text itself (sort.column is checked against SORT_COLUMNS);
    # every value travels separately as a parameter.
    sql = f"SELECT {_COLUMNS} FROM services{where} ORDER BY {order} LIMIT %s OFFSET %s"
    params += [limit, offset]

    with db.transaction(settings, server_id, isolation, on_event) as cur:
        cur.row_factory = class_row(Service)
        cur.execute(sql, params)
        return cur.fetchall()


def read_on(
    settings: Settings,
    server_id: int,
    stop_rdt_id: int,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> Service | None:
    """Read one row from one specific server, with no routing. For checks that compare copies.

    Raises ServerDown if that server can't be reached.
    """
    rows = _query(settings, server_id, Filters(stop_rdt_id=stop_rdt_id), Sort(), None, None, 0, 1, isolation, on_event)
    return rows[0] if rows else None


def _query(settings: Settings, server_id: int, *args: object) -> list[Service]:
    """_select_page, remembering whether the server answered."""
    try:
        rows = _select_page(settings, server_id, *args)
    except db.ServerDown:
        mark_down(server_id)
        raise
    mark_up(server_id)
    return rows


# --- Choosing servers ---------------------------------------------------------------------------


def _complete_sources(filters: Filters) -> list[tuple[int, ...]]:
    """Groups of servers that together hold every row matching the filters, in order of preference."""
    if filters.service_type is not None:
        return [(CENTRAL,), (fragment_for(filters.service_type),)]
    return [(CENTRAL,), (SPRINTER_SERVER, OTHERS_SERVER)]


def _fetch(
    settings: Settings,
    filters: Filters,
    sort: Sort,
    sources: list[tuple[int, ...]],
    *,
    after: tuple | None,
    before: tuple | None,
    offset: int,
    limit: int,
    isolation: str,
    on_event: db.EventHandler | None,
) -> tuple[list[Service], tuple[int, ...]]:
    """Ask the first usable group of servers; merge when it's more than one server."""
    unavailable: set[int] = set()
    for group in sources:
        if not all(usable(settings, server_id) for server_id in group):
            unavailable.update(s for s in group if not usable(settings, s))
            continue
        args = (filters, sort, after, before, offset, limit, isolation, on_event)
        try:
            if len(group) == 1:
                return _query(settings, group[0], *args), group
            # Each fragment returns its own `limit` rows; together they always contain the right ones.
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                futures = [pool.submit(_query, settings, server_id, *args) for server_id in group]
                results = [future.result() for future in futures]
        except db.ServerDown as error:
            unavailable.add(error.server_id)
            continue
        # Both lists are already in the same order, so merging them keeps that order.
        descending = sort.descending != (before is not None)
        merged = heapq.merge(*results, key=sort.key, reverse=descending)
        return list(itertools.islice(merged, limit)), group

    names = ", ".join(f"server {s}" for s in sorted(unavailable))
    raise Unavailable(f"These rows can't be read right now: {names} unavailable.")


# --- Public reads -------------------------------------------------------------------------------


def get_row(
    settings: Settings,
    stop_rdt_id: int,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> RowResult:
    """One row by its ID: this server first, then server 0, then the remaining fragment.

    A fragment saying "not here" may just mean the row belongs to the other fragment, so the search
    continues until server 0, or both fragments, have answered.
    """
    me = settings.server_id
    order = [me] + ([CENTRAL] if me != CENTRAL else []) + [s for s in (SPRINTER_SERVER, OTHERS_SERVER) if s != me]
    by_id = Filters(stop_rdt_id=stop_rdt_id)

    said_no: set[int] = set()
    unavailable: set[int] = set()
    for server_id in order:
        if not usable(settings, server_id):
            unavailable.add(server_id)
            continue
        try:
            rows = _query(settings, server_id, by_id, Sort(), None, None, 0, 1, isolation, on_event)
        except db.ServerDown:
            unavailable.add(server_id)
            continue
        if rows:
            return RowResult(rows[0], (server_id,))
        said_no.add(server_id)
        if CENTRAL in said_no or {SPRINTER_SERVER, OTHERS_SERVER} <= said_no:
            return RowResult(None, tuple(sorted(said_no)))

    names = ", ".join(f"server {s}" for s in sorted(unavailable))
    raise Unavailable(f"Row {stop_rdt_id} can't be looked up right now: {names} unavailable.")


def list_rows(
    settings: Settings,
    filters: Filters = Filters(),
    sort: Sort = Sort(),
    *,
    after: Service | None = None,
    before: Service | None = None,
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> Page:
    """A page of rows in `sort` order.

    First page: no after/before.  Next: after = the last row shown.  Previous: before = the first row shown.
    """
    if after is not None and before is not None:
        raise ValueError("Give after or before, not both.")
    rows, served_by = _fetch(
        settings,
        filters,
        sort,
        _complete_sources(filters),
        after=sort.key(after) if after is not None else None,
        before=sort.key(before) if before is not None else None,
        offset=0,
        limit=PAGE_SIZE + 1,  # one extra row says whether there's another page beyond this one
        isolation=isolation,
        on_event=on_event,
    )
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if before is not None:  # fetched backwards from `before`: turn it the right way round
        rows.reverse()
        return Page(rows, has_previous=more, has_next=True, served_by=served_by)
    return Page(rows, has_previous=after is not None, has_next=more, served_by=served_by)


def can_jump(settings: Settings, filters: Filters = Filters()) -> bool:
    """Whether a typed page number can work now: it needs one server holding every matching row."""
    return any(len(group) == 1 and usable(settings, group[0]) for group in _complete_sources(filters))


def jump_to_page(
    settings: Settings,
    filters: Filters,
    page_number: int,
    sort: Sort = Sort(),
    isolation: str = db.DEFAULT_ISOLATION,
    on_event: db.EventHandler | None = None,
) -> Page:
    """Page N (from 1) via OFFSET: one server skips the rows before it. Deep pages take a few seconds.

    Never merges fragments (each would have to send every row up to that page), so it raises
    Unavailable when no single server holds every matching row: usually, when server 0 is down.
    After a jump, continue with list_rows(after=...) / list_rows(before=...).
    """
    if page_number < 1:
        raise ValueError("Page numbers start at 1.")
    single_servers = [group for group in _complete_sources(filters) if len(group) == 1]
    try:
        rows, served_by = _fetch(
            settings,
            filters,
            sort,
            single_servers,
            after=None,
            before=None,
            offset=(page_number - 1) * PAGE_SIZE,
            limit=PAGE_SIZE + 1,
            isolation=isolation,
            on_event=on_event,
        )
    except Unavailable as error:
        raise Unavailable(
            "Jumping to a page number needs server 0, which is unavailable right now. Use Next / Previous instead."
        ) from error
    return Page(rows[:PAGE_SIZE], has_previous=page_number > 1, has_next=len(rows) > PAGE_SIZE, served_by=served_by)


def value_lists(settings: Settings, on_event: db.EventHandler | None = None) -> tuple[list[str], list[str]]:
    """The service types and companies that exist, for the dropdowns.

    One pass over the table (a few seconds on server 0), so the pages cache the answer. Companies that
    differ only in upper/lower case ("NS", "ns") are listed once, since the company filter ignores case.
    With server 0 down, both fragments are asked and their answers combined.
    """
    sql = "SELECT service_type, min(service_company) FROM services GROUP BY service_type, lower(service_company)"
    for group in ((CENTRAL,), (SPRINTER_SERVER, OTHERS_SERVER)):
        if not all(usable(settings, s) for s in group):
            continue
        try:
            pairs: list[tuple] = []
            for server_id in group:
                with db.transaction(settings, server_id, on_event=on_event) as cur:
                    cur.execute(sql)
                    pairs += cur.fetchall()
                mark_up(server_id)
        except db.ServerDown as error:
            mark_down(error.server_id)
            continue
        types = sorted({service_type for service_type, _ in pairs if service_type is not None})
        companies: dict[str, str] = {}  # lower case -> how it's shown
        for _, company in pairs:
            if company is not None:
                companies[company.lower()] = min(companies.get(company.lower(), company), company)
        return types, sorted(companies.values(), key=str.lower)
    raise Unavailable("The lists of types and companies can't be loaded right now.")
