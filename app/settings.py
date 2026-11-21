"""Loads this machine's settings from .streamlit/secrets.toml (see README -> Configuration).

This is the only module that reads the settings file. Everything else receives the Settings
object it returns, including worker threads, which must never read st.secrets themselves.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# The repo root is one folder above this file (app/settings.py -> repo root).
SECRETS_FILE = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"

SERVER_IDS = (0, 1, 2)


class SettingsError(Exception):
    """The settings file is missing or wrong. The message says what to fix."""


@dataclass(frozen=True)
class ServerAddress:
    """Where one server's database is."""

    server_id: int
    host: str
    port: int


@dataclass(frozen=True)
class Settings:
    server_id: int  # which server this copy of the app is: 0, 1 or 2
    connect_timeout: int  # seconds before a server counts as down
    db_name: str
    db_user: str
    db_password: str
    servers: dict[int, ServerAddress]  # keyed by server number: 0, 1, 2

    @property
    def me(self) -> ServerAddress:
        """This copy of the app's own server."""
        return self.servers[self.server_id]


def load_settings(path: Path = SECRETS_FILE) -> Settings:
    """Read and check the settings file. Raises SettingsError with a plain explanation if it's wrong."""
    if not path.exists():
        raise SettingsError(
            f"{path} not found. Copy .streamlit/secrets.example.toml to .streamlit/secrets.toml "
            "and fill it in (README -> Configuration)."
        )

    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"{path} isn't valid TOML: {error}") from error

    try:
        server_id = int(raw["server_id"])
        connect_timeout = int(raw.get("connect_timeout", 3))
        db = raw["db"]
        db_name, db_user, db_password = str(db["name"]), str(db["user"]), str(db["password"])
        # TOML section names are always text ("0"), but server_id is a number (0): convert the keys.
        servers = {
            int(key): ServerAddress(server_id=int(key), host=str(value["host"]), port=int(value["port"]))
            for key, value in raw["servers"].items()
        }
    except KeyError as error:
        raise SettingsError(f"{path} is missing the setting {error}. Compare it with secrets.example.toml.") from error
    except (TypeError, ValueError) as error:
        raise SettingsError(f"{path} has a setting with the wrong type: {error}") from error

    if sorted(servers) != list(SERVER_IDS):
        raise SettingsError(f"{path} must define exactly [servers.0], [servers.1] and [servers.2].")
    if server_id not in SERVER_IDS:
        raise SettingsError(f"server_id in {path} must be 0, 1 or 2, not {server_id}.")
    if db_password in ("", "CHANGE_ME"):
        raise SettingsError(f"Set the real 'app' password in {path} (it still has the placeholder).")

    return Settings(
        server_id=server_id,
        connect_timeout=connect_timeout,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        servers=servers,
    )
