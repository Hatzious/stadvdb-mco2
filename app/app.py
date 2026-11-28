"""Entry file of the webapp:  uv run streamlit run app/app.py

Sets up what every page shares (the settings check and the isolation level in the sidebar), then
shows the page picked in the navigation menu. The pages themselves are in app/views/.
"""

import streamlit as st

import db
import ui
from settings import SettingsError

st.set_page_config(page_title="STADVDB MCO2", page_icon="🚆", layout="wide")

try:
    ui.get_settings()
except SettingsError as error:
    st.error(str(error))
    st.stop()

with st.sidebar:
    st.selectbox(
        "Isolation level",
        db.ISOLATION_LEVELS,
        index=db.ISOLATION_LEVELS.index(db.DEFAULT_ISOLATION),
        key="isolation",
        help="Used by every transaction the pages run. Replication always uses READ COMMITTED.",
    )
    st.caption("PostgreSQL runs READ UNCOMMITTED exactly like READ COMMITTED: it never shows uncommitted data.")

page = st.navigation([
    st.Page("views/records.py", title="Records", icon="📋", default=True),
    st.Page("views/simulation.py", title="Simulation", icon="🧪"),
    st.Page("views/connection_check.py", title="Connection check", icon="🔌"),
])
page.run()
