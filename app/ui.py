"""Pieces shared by the pages: this server's settings, the isolation level picked in the sidebar,
server names, Netherlands time, and the cached dropdown lists."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import streamlit as st

import access
import db
from settings import Settings, load_settings

SERVER_NAMES = {0: "server 0 (Hera, all rows)", 1: "server 1 (Artemis, Sprinter)", 2: "server 2 (Athena, other types)"}

# Times are stored in UTC; people read and type them in Dutch time (summer time handled automatically).
LOCAL_TIME = ZoneInfo("Europe/Amsterdam")


@st.cache_resource
def get_settings() -> Settings:
    """This server's settings, read once. Raises SettingsError if the file is missing or wrong."""
    return load_settings()


def isolation() -> str:
    """The isolation level picked in the sidebar (app.py), used by every transaction a page runs."""
    return st.session_state.get("isolation", db.DEFAULT_ISOLATION)


def show_time(moment: datetime | None) -> str:
    """A stored time as Dutch local time, e.g. "2021-01-01 02:13"."""
    return "" if moment is None else moment.astimezone(LOCAL_TIME).strftime("%Y-%m-%d %H:%M")


def from_local(day: date, clock: time) -> datetime:
    """A date and time typed in Dutch local time, as an exact moment (stored as UTC by PostgreSQL)."""
    return datetime.combine(day, clock, tzinfo=LOCAL_TIME)


@st.cache_data(ttl=3600, show_spinner="Loading the lists of types and companies (once an hour)...")
def value_lists(_settings: Settings) -> tuple[list[str], list[str]]:
    """The type and company dropdown lists, read from the database at most once an hour.

    Only these labels are cached; every row a page shows is still read fresh. (The leading underscore
    tells Streamlit not to try to hash the settings object.)
    """
    return access.value_lists(_settings)
