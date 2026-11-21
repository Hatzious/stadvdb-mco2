"""Connection check: shows which server this copy of the app is, and whether it can reach all three databases.

Entry file of the webapp:  uv run streamlit run app/app.py
"""

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg
import streamlit as st

from settings import ServerAddress, Settings, SettingsError, load_settings

SERVER_NAMES = {0: "server 0 (Hera, all rows)", 1: "server 1 (Artemis, Sprinter)", 2: "server 2 (Athena, other types)"}


@dataclass
class CheckResult:
    server_id: int
    ok: bool
    detail: str  # the server's address and row estimate if it worked, the error if not
    millis: int


def check_server(settings: Settings, server: ServerAddress) -> CheckResult:
    """Connect to one server's database and ask it a few cheap questions. Runs in a worker thread."""
    started = time.perf_counter()
    try:
        with psycopg.connect(
            host=server.host,
            port=server.port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=settings.connect_timeout,
        ) as conn:
            address, rows = conn.execute(
                # reltuples is Postgres's own row estimate: instant, unlike count(*) on millions of rows.
                "SELECT inet_server_addr()::text, reltuples::bigint FROM pg_class WHERE oid = 'services'::regclass"
            ).fetchone()
        detail = f"answered from {address}, about {rows:,} rows in services"
        ok = True
    except psycopg.Error as error:
        detail = str(error).strip().splitlines()[0]
        ok = False
    return CheckResult(server.server_id, ok, detail, round((time.perf_counter() - started) * 1000))


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

# Check all three at once, so one server that's down doesn't make the others wait.
with st.spinner("Contacting the three databases..."):
    with ThreadPoolExecutor(max_workers=len(settings.servers)) as pool:
        results = list(pool.map(lambda server: check_server(settings, server), settings.servers.values()))

for result in results:
    server = settings.servers[result.server_id]
    label = f"**{SERVER_NAMES[result.server_id]}** at `{server.host}:{server.port}`"
    if result.ok:
        st.success(f"{label}: reachable in {result.millis} ms, {result.detail}")
    else:
        st.error(f"{label}: NOT reachable after {result.millis} ms. {result.detail}")

st.button("Check again")
