"""Connection check: shows which server this copy of the app is, and whether it can reach all three databases.

Entry file of the webapp:  uv run streamlit run app/app.py
"""

import socket
from dataclasses import asdict

import streamlit as st

from access import Unavailable, list_rows
from db import ping_all
from settings import SettingsError, load_settings

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
try:
    page = list_rows(settings)
except Unavailable as error:
    st.error(str(error))
else:
    answered = " and ".join(SERVER_NAMES[s] for s in page.served_by)
    st.write(f"First {len(page.rows)} rows in ID order, answered by **{answered}**.")
    st.table([asdict(row) for row in page.rows])
