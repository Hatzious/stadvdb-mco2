"""Connection check: which server this copy of the app is, whether it reaches all three databases,
and the state of replication (with the "Replay now" button, until the Node monitor page exists)."""

import socket

import streamlit as st

import access
import replication
import ui
from db import ping_all

settings = ui.get_settings()
SERVER_NAMES = ui.SERVER_NAMES

st.title("Connection check")
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

st.subheader("Replication")
st.caption(
    f"The replay service on each server delivers its own log every {replication.REPLAY_INTERVAL_SECONDS} s. "
    "A server is *recovering* while another server's log holds writes it missed; reads and writes skip it."
)
if st.button("Replay now"):
    with st.spinner("Delivering every log to every server..."):
        replayed = replication.replay_all(settings)
    done = [r for r in replayed if r.applied or r.conflicts]
    for r in done:
        st.success(f"Server {r.source}'s log -> server {r.target}: applied {len(r.applied)}, conflicts {len(r.conflicts)}")
    for r in replayed:
        if r.stopped:
            st.warning(f"Server {r.source}'s log -> server {r.target}: {r.stopped}")
    if not done and not any(r.stopped for r in replayed):
        st.info("Nothing was pending.")

up_servers = {r.server_id for r in results if r.up}
st.table([
    {
        "server": SERVER_NAMES[s],
        "state": "down" if s not in up_servers else ("ready" if access.is_ready(settings, s) else "RECOVERING"),
    }
    for s in sorted(settings.servers)
])
summaries, unreadable = replication.log_summaries(settings)
if summaries:
    st.table([
        {
            "log on": SERVER_NAMES[x.source],
            "for": SERVER_NAMES[x.target],
            "pending": x.pending,
            "conflicts": x.conflicts,
            "oldest pending (NL time)": ui.show_time(x.oldest_pending),
        }
        for x in summaries
    ])
else:
    st.write("All readable logs are empty.")
for s in unreadable:
    st.warning(f"Couldn't read the log on {SERVER_NAMES[s]}.")
