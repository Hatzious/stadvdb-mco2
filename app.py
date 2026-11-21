"""Hello-world page: proves nginx -> Streamlit works on each server. Replaced by the real app later."""

import platform
import socket

import streamlit as st

st.set_page_config(page_title="STADVDB MCO2", page_icon="🚆")

st.title("Hello from STADVDB MCO2")
st.write(f"This page is served by **{socket.gethostname()}**.")
st.caption(f"Python {platform.python_version()} · Streamlit {st.__version__}")
