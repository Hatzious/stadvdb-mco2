"""The replay service: every REPLAY_INTERVAL_SECONDS, deliver this server's log to the other two servers.

Runs as its own systemd service (deploy/stadvdb-replay.service), separate from the webapp, so it works
from the moment the server boots, even when nobody has a page open. Each server only delivers its own
log; the advisory lock in replication.deliver keeps it from clashing with deliveries started by a page.

Run by hand, from the repo folder (Ctrl+C to stop):
    uv run python app/replay_worker.py
Its output goes to the service log:
    sudo journalctl -u stadvdb-replay -f
"""

from __future__ import annotations

import time
from datetime import datetime

from replication import REPLAY_INTERVAL_SECONDS, deliver
from settings import SettingsError, load_settings


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}", flush=True)


def replay_once(settings) -> None:
    me = settings.server_id
    for target in sorted(settings.servers):
        if target == me:
            continue
        result = deliver(settings, source=me, target=target)
        # Only report when something happened, so an idle log stays quiet.
        if result.applied or result.conflicts:
            log(f"server {me} -> server {target}: applied {len(result.applied)}, conflicts {len(result.conflicts)}"
                + (f" (conflict entries {result.conflicts})" if result.conflicts else ""))
        if result.stopped:
            log(f"server {me} -> server {target}: waiting ({result.stopped})")


def main() -> None:
    try:
        settings = load_settings()
    except SettingsError as error:
        log(f"Can't start: {error}")
        raise SystemExit(1)

    log(f"Replay service for server {settings.server_id}: delivering its log every {REPLAY_INTERVAL_SECONDS} s")
    while True:
        started = time.monotonic()
        try:
            replay_once(settings)
        except Exception as error:  # never let one bad round stop the service
            log(f"Replay round failed: {type(error).__name__}: {error}")
        time.sleep(max(0.0, REPLAY_INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
