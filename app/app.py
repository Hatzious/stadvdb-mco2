"""Connection check: shows which server this copy of the app is, and whether it can reach all three databases.

Entry file of the webapp:  uv run streamlit run app/app.py
"""

import socket
from dataclasses import asdict, replace
from datetime import datetime, timezone

import streamlit as st

from access import CENTRAL, Unavailable, fragment_for, get_row, list_rows, read_on
from db import Event, Refused, ServerDown, ping_all
from replication import get_entry
from settings import SettingsError, load_settings
from writes import WriteResult, delete_row, insert_row, update_row

SERVER_NAMES = {0: "server 0 (Hera, all rows)", 1: "server 1 (Artemis, Sprinter)", 2: "server 2 (Athena, other types)"}

st.set_page_config(page_title="STADVDB MCO2", page_icon="🚆")
st.title("Connection check")

try:
    settings = load_settings()
except SettingsError as error:
    st.error(str(error))
    st.stop()

st.write(
    f"This copy of the app runs on **{socket.gethostname()}** "
    f"and acts as **{SERVER_NAMES[settings.server_id]}**."
)

with st.spinner("Contacting the three databases..."):
    results = ping_all(settings)

for result in results:
    server = settings.servers[result.server_id]
    label = f"**{SERVER_NAMES[result.server_id]}** at `{server.host}:{server.port}`"
    if result.up:
        st.success(f"{label}: reachable in {result.millis} ms, {result.detail}")
    else:
        st.error(f"{label}: NOT reachable after {result.millis} ms. {result.detail}")

st.button("Check again")

# Temporary check of the read routing (access.py) against the real databases, until the Records page exists.
st.subheader("Sample read")
first_id = 1
try:
    page = list_rows(settings)
except Unavailable as error:
    st.error(str(error))
else:
    answered = " and ".join(SERVER_NAMES[s] for s in page.served_by)
    st.write(f"First {len(page.rows)} rows in ID order, answered by **{answered}**.")
    st.table([asdict(row) for row in page.rows])
    if page.rows:
        first_id = page.rows[0].stop_rdt_id


# Temporary check of the write path (writes.py, replication.py), until the Records page exists.
st.subheader("Write test")
st.caption("Changes real rows on both copies. Stop PostgreSQL on a server to see the log entry stay pending.")

PROBLEMS = (Refused, ServerDown, Unavailable)  # EditConflict is a kind of Refused


def show_copies(stop_rdt_id: int, service_type: str | None) -> None:
    """The row on server 0 and on its fragment, side by side."""
    copies = []
    for server_id in (CENTRAL, fragment_for(service_type)):
        try:
            row = read_on(settings, server_id, stop_rdt_id)
            values = asdict(row) if row else {"stop_rdt_id": "(no row)"}
        except ServerDown as error:
            values = {"stop_rdt_id": f"(unreachable: {error.reason})"}
        copies.append({"copy on": SERVER_NAMES[server_id], **values})
    st.table(copies)


def show_result(result: WriteResult, events: list[Event]) -> None:
    other = {"applied": "up to date", "pending": "PENDING until the replay", "conflict": "in CONFLICT"}[result.other_copy]
    st.success(
        f"{result.operation.capitalize()} committed on {SERVER_NAMES[result.primary]}; "
        f"the copy on {SERVER_NAMES[result.secondary]} is {other}."
    )
    if result.delivery.stopped:
        st.warning(f"Delivery stopped: {result.delivery.stopped}")
    try:
        entry = get_entry(settings, result.primary, result.entry_id)
        st.write(f"Log entry #{result.entry_id} on {SERVER_NAMES[result.primary]}:")
        st.table([asdict(entry)])
    except ServerDown as error:
        st.warning(f"Couldn't read the log entry: {error.reason}")
    start = events[0].at if events else 0.0
    st.write("Steps, in order:")
    st.table([
        {"ms": round((e.at - start) * 1000), "server": e.server_id, "step": e.step, "detail": e.detail}
        for e in events
    ])


edit_tab, insert_tab = st.tabs(["Edit a row's delay", "Insert and delete a test row"])

with edit_tab:
    row_id = int(st.number_input("Row ID", min_value=1, step=1, value=first_id))
    try:
        found = get_row(settings, row_id).row
    except Unavailable as error:
        st.error(str(error))
        found = None
    if found is None:
        st.info("No row with this ID (or it can't be read right now).")
    else:
        st.write("Before:")
        show_copies(found.stop_rdt_id, found.service_type)
        new_delay = int(st.number_input("New delay (minutes)", step=1, value=found.stop_arrival_delay or 0))
        if st.button("Save delay"):
            events: list[Event] = []
            try:
                result = update_row(settings, found, replace(found, stop_arrival_delay=new_delay), on_event=events.append)
            except PROBLEMS as error:
                st.error(str(error))
            else:
                show_result(result, events)
                st.write("After:")
                show_copies(found.stop_rdt_id, found.service_type)

with insert_tab:
    test_type = st.selectbox("Type of the test row", ["Sprinter", "Intercity"])
    if st.button("Insert test row"):
        events = []
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            result = insert_row(settings, test_type, "TEST", now, 0, on_event=events.append)
        except PROBLEMS as error:
            st.error(str(error))
        else:
            st.session_state["test_row"] = (result.stop_rdt_id, test_type)
            show_result(result, events)

    test_row = st.session_state.get("test_row")
    if test_row:
        test_id, test_row_type = test_row
        st.write(f"Test row **{test_id}**, now:")
        show_copies(test_id, test_row_type)
        if st.button("Delete test row"):
            events = []
            try:
                current = get_row(settings, test_id).row
                if current is None:
                    st.info("It's already gone.")
                else:
                    show_result(delete_row(settings, current, on_event=events.append), events)
                    st.write("After:")
                    show_copies(test_id, test_row_type)
                del st.session_state["test_row"]
            except PROBLEMS as error:
                st.error(str(error))
