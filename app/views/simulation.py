"""Simulation page: concurrency and replication cases on the three-node cluster.

Case 1 (implemented here): two transactions read the same row at the same time, one pinned to
server 0 and one to the fragment holding the row. Each reads twice with a pause in between, inside
the same transaction, so the timeline shows that readers never block each other and that both see
the same committed value at every isolation level.

"""

from __future__ import annotations

import csv
import io
import queue
import threading
import time
from dataclasses import dataclass

import streamlit as st

import access
import db
import ui
from settings import Settings

READ_SQL = "SELECT service_type, stop_arrival_delay FROM services WHERE stop_rdt_id = %s"
START_GAP_SECONDS = 0.3  # T2 starts just after T1, so the overlap is easy to see in the timeline


@dataclass(frozen=True)
class Reading:
    """What one of the two concurrent transactions saw."""

    name: str
    server_id: int
    values: tuple[object | None, ...]  # the (type, delay) pair from each of its two reads
    outcome: str  # "same value in both reads", "value changed", or the error
    ok: bool


@dataclass(frozen=True)
class Run:
    """One recorded Case 1 run, kept in session state for the report."""

    case: int
    level: str
    row_id: int
    fragment: int
    timeline: list[tuple[float, str, int, str, str]]  # time, transaction, server, step, detail
    readings: list[Reading]
    copies: dict[int, access.Service | None]
    error: str = ""

    @property
    def consistent(self) -> bool:
        """Case 1 passed: both readers agree with themselves and the two copies match."""
        if self.error or any(not reading.ok for reading in self.readings):
            return False
        copies = list(self.copies.values())
        return len(copies) == 2 and all(copy is not None for copy in copies) and copies[0] == copies[1]


def _describe(value: tuple[object | None, ...] | None) -> str:
    """A read result as one line of timeline detail."""
    if value is None:
        return "row not present on this server"
    service_type, delay = value
    shown_delay = "(none)" if delay is None else f"{delay} min"
    return f"type={service_type or '(none)'}, delay={shown_delay}"


def _read_twice(
    settings: Settings,
    server_id: int,
    row_id: int,
    level: str,
    pause: float,
    name: str,
    events: queue.Queue[tuple[float, str, int, str, str]],
) -> Reading:
    """One transaction: read, pause, read again. Events go to the queue, never to Streamlit."""

    def report(event: db.Event) -> None:
        events.put((event.at, name, event.server_id, event.step, event.detail))

    values: list[tuple[object | None, ...]] = []
    try:
        with db.transaction(settings, server_id=server_id, isolation=level, on_event=report) as cur:
            for number in (1, 2):
                cur.execute(READ_SQL, (row_id,))
                row = cur.fetchone()
                value = (row[0], row[1]) if row else None
                values.append(value)
                db.emit(report, server_id, f"read {number}", _describe(value))
                if number == 1:
                    # Hold this transaction open so the other reader definitely overlaps it.
                    cur.execute("SELECT pg_sleep(%s)", (pause,))
    except Exception as error:  # ServerDown, Refused, or a bug: all of it belongs in the timeline
        outcome = f"{type(error).__name__}: {error}"
        events.put((time.time(), name, server_id, type(error).__name__, str(error)))
        return Reading(name, server_id, tuple(values), outcome, False)

    same = values[0] == values[1]
    outcome = "same value in both reads" if same else "value changed between the reads"
    events.put((time.time(), name, server_id, "result", outcome))
    return Reading(name, server_id, tuple(values), outcome, same)


def run_case1(settings: Settings, row: access.Service, level: str, pause: float) -> Run:
    """Run two readers concurrently, then compare the two physical copies of the row."""
    fragment = access.fragment_for(row.service_type)
    server_ids = (0, fragment)
    events: queue.Queue[tuple[float, str, int, str, str]] = queue.Queue()
    readings: dict[str, Reading] = {}

    def worker(name: str, server_id: int) -> None:
        readings[name] = _read_twice(settings, server_id, row.stop_rdt_id, level, pause, name, events)

    threads = [
        threading.Thread(target=worker, args=(f"T{number}", server_id))
        for number, server_id in enumerate(server_ids, 1)
    ]
    threads[0].start()
    time.sleep(START_GAP_SECONDS)
    for thread in threads[1:]:
        thread.start()
    for thread in threads:
        thread.join()

    timeline: list[tuple[float, str, int, str, str]] = []
    while not events.empty():
        timeline.append(events.get())

    copies: dict[int, access.Service | None] = {}
    error = ""
    for server_id in server_ids:
        try:
            copies[server_id] = access.read_on(settings, server_id, row.stop_rdt_id, isolation=level)
        except (db.ServerProblem, access.Unavailable) as problem:
            copies[server_id] = None
            error = f"Couldn't compare copies: {problem}"

    return Run(
        1, level, row.stop_rdt_id, fragment, sorted(timeline),
        [readings[f"T{number}"] for number in (1, 2)], copies, error,
    )


def _runs_csv(runs: list[Run]) -> str:
    """A downloadable timeline for the report, with times relative to the first event."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["run", "case", "isolation_level", "row_id", "time_ms", "transaction", "server", "step", "detail"])
    for number, run in enumerate(runs, 1):
        begin = run.timeline[0][0] if run.timeline else 0.0
        for at, name, server_id, step, detail in run.timeline:
            writer.writerow([
                number, run.case, run.level, run.row_id, round((at - begin) * 1000),
                name, server_id, step, detail,
            ])
    return output.getvalue()


# --- Page ---------------------------------------------------------------------------------------

settings = ui.get_settings()
level = ui.isolation()
state = st.session_state
state.setdefault("sim_runs", [])

st.title("Simulation")
st.subheader("Case 1 — concurrent reads of the same row")
st.write(
    "Two transactions read the same row at the same time: one on server 0 (which holds every row) "
    "and one on the fragment that holds the row. Each reads twice with a pause in between. "
    "Readers never block readers, so both transactions should finish and see the same committed "
    "value at every isolation level."
)
st.caption(
    f"Isolation level for these transactions: **{level}** (set in the sidebar). "
    "Replication delivery always runs at READ COMMITTED."
)

try:
    sample = access.list_rows(settings, access.Filters(service_type=access.SPRINTER), access.Sort(), isolation=level)
    default_row_id = sample.rows[0].stop_rdt_id if sample.rows else 1
except (access.Unavailable, db.ServerProblem):
    default_row_id = 1  # the suggested ID is only a convenience; the user can type any ID

id_col, pause_col, run_col = st.columns([1, 1, 2])
row_id = id_col.number_input(
    "Demo row ID", min_value=1, value=state.get("sim_row_id", default_row_id), step=1,
    help="Pick an ID from the Records page; every row has a copy on server 0 and one fragment.",
)
pause = pause_col.number_input("Pause between reads (s)", min_value=1.0, max_value=10.0, value=3.0, step=0.5)
start_run = run_col.button("▶ Run case 1", type="primary")

if start_run:
    state["sim_row_id"] = row_id
    try:
        found = access.get_row(settings, row_id, isolation=level)
    except (access.Unavailable, db.ServerProblem) as problem:
        st.error(f"Can't look up row {row_id}: {problem}")
        st.stop()
    if found.row is None:
        st.error(f"Row {row_id} doesn't exist on any reachable server. Pick another ID from the Records page.")
        st.stop()
    with st.spinner("Running two readers at the same time..."):
        result = run_case1(settings, found.row, level, float(pause))
    state["sim_runs"].append(result)

if state["sim_runs"]:
    latest = state["sim_runs"][-1]
    st.divider()
    st.write(f"**Run {len(state['sim_runs'])}** — case 1 · {latest.level} · row {latest.row_id}")
    st.caption(
        "Readers: "
        + ", ".join(f"{reading.name} on {ui.SERVER_NAMES[reading.server_id]}" for reading in latest.readings)
        + f". Row owned by {ui.SERVER_NAMES[latest.fragment]}."
    )
    if latest.consistent:
        st.success("Consistent: both readers saw the same value twice and the two copies of the row match.")
    else:
        st.warning("Not all checks passed — see the timeline and copies below.")
    if latest.error:
        st.error(latest.error)

    st.markdown("**Timeline**")
    begin = latest.timeline[0][0] if latest.timeline else 0.0
    st.dataframe(
        [
            {
                "Time": f"+{(at - begin) * 1000:.0f} ms",
                "Transaction": name,
                "Server": ui.SERVER_NAMES.get(server_id, f"server {server_id}"),
                "Step": step,
                "Detail": detail,
            }
            for at, name, server_id, step, detail in latest.timeline
        ],
        hide_index=True,
        width="stretch",
    )
    for reading in latest.readings:
        st.caption(f"{reading.name} ({ui.SERVER_NAMES[reading.server_id]}): {reading.outcome}.")

    st.markdown("**Copies after the run**")
    st.dataframe(
        [
            {
                "Server": ui.SERVER_NAMES[server_id],
                "Present": "yes" if copy is not None else "not readable",
                "Type": (copy.service_type or "(none)") if copy is not None else "—",
                "Delay (min)": (
                    "(none)" if copy is None or copy.stop_arrival_delay is None else copy.stop_arrival_delay
                ),
            }
            for server_id, copy in sorted(latest.copies.items())
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(f"{len(state['sim_runs'])} run(s) recorded in this session (change the isolation level and run again).")
    st.download_button(
        "Download case 1 runs (CSV)", data=_runs_csv(state["sim_runs"]), file_name="case1_runs.csv",
        mime="text/csv", key="case1_runs_csv",
    )
