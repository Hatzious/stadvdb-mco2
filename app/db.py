"""Connection module: the only file that talks to PostgreSQL directly.

Everything else asks for a server by number (0, 1, 2) and gets back either results or one of two errors:

    ServerDown  - couldn't reach the server, or lost it mid-way.   Replication rule: continue on the other copy and log it.
    Refused     - the server answered "no" (conflict, lock timeout, broken constraint).   Rule: cancel the whole write.

Usage:

    with transaction(settings, server_id=0, isolation="READ COMMITTED") as cur:
        cur.execute("SELECT ... WHERE stop_rdt_id = %s", (row_id,))
        row = cur.fetchone()

Each transaction opens its own connection and closes it at the end (no pool). That keeps every
transaction independent, which is what the concurrency demos need: each worker thread gets its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg

from settings import Settings

# The four SQL isolation levels, as written after BEGIN ISOLATION LEVEL.
# PostgreSQL runs READ UNCOMMITTED exactly like READ COMMITTED (it never allows dirty reads).
ISOLATION_LEVELS = ("READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE")
DEFAULT_ISOLATION = "READ COMMITTED"  # PostgreSQL's own default

# Longest a transaction waits for a lock (a row another transaction is changing, or the delivery lock)
# before giving up with "lock not available", which arrives as Refused. Longer than any Simulation pause.
LOCK_TIMEOUT = "20s"

# Notice a dead server within seconds instead of minutes. A stopped PostgreSQL refuses connections at once,
# but a powered-off container never answers, and without these the app could hang until the OS gives up.
_DEAD_SERVER_OPTIONS = {
    "keepalives": 1,  # send small "are you there?" packets on idle connections...
    "keepalives_idle": 5,  # ...after 5 s of silence,
    "keepalives_interval": 2,  # every 2 s,
    "keepalives_count": 2,  # and give up after 2 unanswered ones.
    "tcp_user_timeout": 5000,  # give up if data we sent isn't acknowledged within 5 s (milliseconds)
}

# PostgreSQL error codes (SQLSTATE) that mean "the server is going away or not ready", not "no".
_DOWN_CODES = {
    "57P01",  # admin_shutdown: the server is stopping
    "57P02",  # crash_shutdown: another server process crashed
    "57P03",  # cannot_connect_now: the server is still starting up or recovering
}
_DOWN_CLASSES = ("08",)  # connection exceptions

# Error classes that mean "the server is fine, but it says no to this transaction".
_REFUSED_CLASSES = (
    "22",  # data exception: a value the column can't hold
    "23",  # integrity constraint violation: duplicate key, NOT NULL, ...
    "40",  # transaction rollback: serialization failure (40001), deadlock (40P01)
    "55",  # object not in prerequisite state: lock not available (55P03)
)
_REFUSED_CODES = {"57014"}  # query_canceled: statement_timeout ran out


class ServerProblem(Exception):
    """Base class for the two outcomes the replication rules care about."""

    def __init__(self, server_id: int, message: str, sqlstate: str | None = None):
        super().__init__(f"server {server_id}: {message}")
        self.server_id = server_id
        self.reason = message  # the same message without the "server N:" prefix
        self.sqlstate = sqlstate  # PostgreSQL's error code, if it sent one


class ServerDown(ServerProblem):
    """Couldn't reach the server, or lost the connection mid-way.

    This includes losing the connection during COMMIT, when nobody knows whether the commit happened.
    That's safe in this design: the replay's before/after check sorts out either outcome.
    """


class Refused(ServerProblem):
    """The server is up but rejected the transaction. Can happen to reads too, under SERIALIZABLE."""


@dataclass(frozen=True)
class Event:
    """One step of a transaction, for the Simulation timeline and the Node monitor."""

    server_id: int
    step: str  # "connecting", "unreachable", "connected", "begin", "committed", "rolled back"
    detail: str = ""
    at: float = field(default_factory=time.time)  # when it happened (seconds since 1970)


# A function that receives events. It may be called from worker threads, so it must be thread-safe
# (putting the event on a queue.Queue is).
EventHandler = Callable[[Event], None]


def emit(on_event: EventHandler | None, server_id: int, step: str, detail: str = "") -> None:
    """Report one step to on_event, if anyone is listening."""
    if on_event is not None:
        on_event(Event(server_id, step, detail))


def _first_line(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


def classify(server_id: int, error: psycopg.Error, conn: psycopg.Connection | None = None) -> ServerProblem | None:
    """Sort a psycopg error into ServerDown or Refused.

    Returns None for anything else (a typo in our SQL, a missing table, ...). Those are bugs in the
    app, not database behaviour, so the caller re-raises them unchanged and they show up loudly.
    """
    code = error.sqlstate
    message = _first_line(error)

    if (conn is not None and conn.broken) or code in _DOWN_CODES or (code or "").startswith(_DOWN_CLASSES):
        return ServerDown(server_id, message, code)
    if code is None and isinstance(error, psycopg.OperationalError):
        return ServerDown(server_id, message, code)  # e.g. "connection refused", "timeout expired"
    if code in _REFUSED_CODES or (code or "").startswith(_REFUSED_CLASSES):
        return Refused(server_id, message, code)
    return None


def connect(settings: Settings, server_id: int, on_event: EventHandler | None = None) -> psycopg.Connection:
    """Open a connection to one server. Raises ServerDown if it can't be reached.

    The connection is in autocommit mode: nothing is a transaction until transaction() sends BEGIN.
    A wrong password also lands here as ServerDown; its message says so, and the connection check page shows it.
    """
    server = settings.servers[server_id]
    emit(on_event, server_id, "connecting", f"{server.host}:{server.port}")
    try:
        conn = psycopg.connect(
            host=server.host,
            port=server.port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=settings.connect_timeout,
            autocommit=True,
            # Read and send text as UTF-8. The load scripts create the databases as UTF8,
            # so this changes nothing there; on an old SQL_ASCII database it stops psycopg from returning
            # text as raw bytes.
            client_encoding="UTF8",
            # Shows up in pg_stat_activity, so the Node monitor can tell which app copy a connection came from.
            application_name=f"stadvdb-app (from server {settings.server_id})",
            **_DEAD_SERVER_OPTIONS,
        )
    except psycopg.Error as error:
        emit(on_event, server_id, "unreachable", _first_line(error))
        raise ServerDown(server_id, _first_line(error), error.sqlstate) from error
    emit(on_event, server_id, "connected")
    return conn


@contextmanager
def transaction(
    settings: Settings,
    server_id: int,
    isolation: str = DEFAULT_ISOLATION,
    on_event: EventHandler | None = None,
) -> Iterator[psycopg.Cursor]:
    """Run one transaction on one server and give the caller a cursor to run queries with.

    Commits if the block finishes normally. If anything goes wrong, nothing is committed: the connection
    is closed and PostgreSQL throws away an unfinished transaction when its connection closes.
    Raises ServerDown or Refused for database trouble; other errors pass through unchanged.
    """
    if isolation not in ISOLATION_LEVELS:
        raise ValueError(f"Unknown isolation level {isolation!r}. Use one of {ISOLATION_LEVELS}.")

    conn = connect(settings, server_id, on_event)
    committed = False
    try:
        try:
            conn.execute(f"BEGIN ISOLATION LEVEL {isolation}")  # safe: isolation was checked against the list
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")  # LOCAL: only for this transaction
            emit(on_event, server_id, "begin", isolation)
            with conn.cursor() as cur:
                yield cur
            # If a query inside the block failed and the error was swallowed, PostgreSQL answers COMMIT
            # with ROLLBACK instead of an error. Don't report that as a success.
            if conn.execute("COMMIT").statusmessage != "COMMIT":
                raise Refused(server_id, "the transaction had an error, so PostgreSQL rolled it back")
            committed = True
            emit(on_event, server_id, "committed")
        except psycopg.Error as error:
            problem = classify(server_id, error, conn)
            if problem is None:
                raise
            raise problem from error
    finally:
        conn.close()
        if not committed:
            emit(on_event, server_id, "rolled back")


@dataclass(frozen=True)
class PingResult:
    server_id: int
    up: bool
    millis: int
    detail: str  # the server's address and row estimate if up, the reason if not


def ping(settings: Settings, server_id: int) -> PingResult:
    """Health check: connect to one server and ask it a cheap question."""
    started = time.perf_counter()
    try:
        with transaction(settings, server_id) as cur:
            # reltuples is PostgreSQL's own row estimate: instant, unlike count(*) on millions of rows.
            cur.execute(
                "SELECT inet_server_addr()::text, reltuples::bigint FROM pg_class WHERE oid = 'services'::regclass"
            )
            address, rows = cur.fetchone()
        up, detail = True, f"answered from {address}, about {rows:,} rows in services"
    except ServerProblem as error:
        up, detail = False, error.reason
    except psycopg.Error as error:  # e.g. the services table doesn't exist on that server
        up, detail = False, _first_line(error)
    return PingResult(server_id, up, round((time.perf_counter() - started) * 1000), detail)


def ping_all(settings: Settings) -> list[PingResult]:
    """Ping every server at the same time, so one that's down doesn't make the others wait."""
    with ThreadPoolExecutor(max_workers=len(settings.servers)) as pool:
        return list(pool.map(lambda server_id: ping(settings, server_id), sorted(settings.servers)))
