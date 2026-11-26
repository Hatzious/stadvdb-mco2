# Handoff: Simulation page, Node monitor and testing

This guide is for whoever builds the remaining parts of the webapp. Everything below the pages
(fragmentation, routing, writes, replication, recovery) is built, deployed and tested on the servers.
What's left:

1. **Simulation page**: spec step 3, concurrency Cases 1–3 under every isolation level.
2. **Node monitor page**: a live view of the three servers, also used to show step 4 (crash and recovery).
3. **Running the cases and collecting results for the report.**

Read the [README](../README.md) first for setup, the SSH tunnel, `secrets.toml` and the deploy workflow.

---

## 1. How the system works (5-minute version)

| Server | Holds | Address |
|---|---|---|
| server 0 (Hera) | every row (central node) | 10.20.200.160 |
| server 1 (Artemis) | `service_type = 'Sprinter'` | 10.20.200.161 |
| server 2 (Athena) | every other type | 10.20.200.162 |

Every row has **two copies**: one on server 0, one on its fragment. Each server runs its own copy of
the app (`stadvdb-app` service) and a **replay service** (`stadvdb-replay`).

**Reads** ([app/access.py](../app/access.py)): this server if it holds the row, else server 0, else the
owning fragment. Lists come from server 0, or from both fragments merged when server 0 is down.

**Writes** ([app/writes.py](../app/writes.py)):

1. Pick the **primary**: server 0 if usable, otherwise the fragment. The other copy is the secondary.
2. One transaction on the primary: the data change **with a before-check** (only applies if the row
   still holds the values the user saw; otherwise `EditConflict`), then a `replication_log` entry for
   the secondary. Commit.
3. **Deliver** the entry to the secondary straight away ([app/replication.py](../app/replication.py)).
   If the secondary is down, the entry stays `pending`.

**Replication log**: each server's `replication_log` table holds entries for the other copy, with
`init_` (before) and `fin_` (after) values. Delivery applies pending entries **oldest first**, under an
**advisory lock** per target (one delivery at a time), with an `init_`/`fin_` check:
target holds `init_` → apply; already holds `fin_` → just mark applied; anything else → `conflict`.

**Recovery**: the replay service delivers each server's log every 40 s (also: **Replay now** button,
and right after every logged write). A server is **recovering** while another server's log holds
entries for it older than 5 s; reads and writes skip it until it has caught up.

**Assumptions** (state these in the report): one server fails at a time; we simulate crashes, not
network partitions.

---

## 2. Code map

| File | What it's for |
|---|---|
| [app/app.py](../app/app.py) | Entry file: sidebar (isolation level) and the page menu (`st.navigation`). **Register new pages here.** |
| [app/views/records.py](../app/views/records.py) | Records page: the user-facing app (never shows servers). |
| [app/views/connection_check.py](../app/views/connection_check.py) | Diagnostics, plus the Replication section and **Replay now** (move these to the Node monitor). |
| [app/ui.py](../app/ui.py) | Shared helpers: `get_settings()`, `isolation()`, `SERVER_NAMES`, `show_time()` (Dutch time). |
| [app/db.py](../app/db.py) | Connections and transactions, the two error types, events, `ping_all`. |
| [app/access.py](../app/access.py) | Reads and routing, `read_on`, `is_ready`, `Service`, `Filters`, `Sort`. |
| [app/writes.py](../app/writes.py) | `update_row`, `delete_row`, `insert_row`, `EditConflict`. |
| [app/replication.py](../app/replication.py) | `deliver`, `replay_all`, `log_summaries`, `get_entry`. |
| [app/replay_worker.py](../app/replay_worker.py) | The 40-second replay service. |

**Rules to keep** (the consistency of the whole system depends on them):

- Change rows **only through `writes.py`**. A direct `UPDATE services ...` on one server changes one copy
  and not the other. (Fine as a deliberate experiment, but say so in the results.)
- Inside a write: **data change first, then the log entry**, in the same transaction.
- Writes go to **server 0 first** while it's usable (fixed order prevents deadlocks across servers).
- Delivery always runs at **READ COMMITTED**; only the transactions being studied use the chosen level.
- Never call `st.*` from a worker thread (see section 3).

---

## 3. Building blocks for the Simulation

```python
import access, db, writes, ui
from access import Service

settings = ui.get_settings()
level = ui.isolation()                       # the level picked in the sidebar
```

**A hand-written transaction on one specific server:**

```python
with db.transaction(settings, server_id=1, isolation=level, on_event=report) as cur:
    cur.execute("SELECT stop_arrival_delay FROM services WHERE stop_rdt_id = %s", (row_id,))
    first = cur.fetchone()[0]
    cur.execute("SELECT pg_sleep(3)")        # hold the transaction open so others overlap it
    cur.execute("SELECT stop_arrival_delay FROM services WHERE stop_rdt_id = %s", (row_id,))
    second = cur.fetchone()[0]               # same as `first`? depends on the isolation level
```

Commits at the end of the block; if anything fails, nothing is committed. Always pass values as
parameters (`%s`), never pasted into the SQL text.

**Routed writes** (the real write path, with replication):

```python
row = access.get_row(settings, row_id, isolation=level).row          # what the "user" sees
result = writes.update_row(settings, row, Service(..., stop_arrival_delay=8), isolation=level, on_event=report)
result.primary, result.secondary, result.other_copy                  # e.g. 0, 1, "applied"
```

**Holding a write's transaction open** without changing `writes.py`: the `on_event` callback runs
inside the transaction, and the step `"logged"` comes right before the commit. Sleeping there keeps
the row lock held:

```python
def slow(event):
    report(event)
    if event.step == "logged":
        time.sleep(3)                        # the row stays locked for 3 s
```

**Did all copies end up the same?**

```python
copy0 = access.read_on(settings, 0, row_id)
copyf = access.read_on(settings, access.fragment_for(row.service_type), row_id)
consistent = copy0 == copyf
```

**Errors to catch:**

| Exception | Meaning |
|---|---|
| `writes.EditConflict` | the before-check failed: the row changed since it was read |
| `db.Refused` | the server said no: serialization failure (`sqlstate` 40001), deadlock (40P01), lock timeout after 20 s (55P03) |
| `db.ServerDown` | a server couldn't be reached |
| `access.Unavailable` | no copy of the data can be reached |

`EditConflict` is a kind of `Refused`, so catch it first.

**Running transactions at the same time:** Streamlit reruns the whole page script on every click, and
`st.*` only works in the main thread. Pattern:

```python
import queue, threading, time

events = queue.Queue()

def run(name, job):
    def report(event):
        events.put((time.time(), name, event.server_id, event.step, event.detail))
    try:
        job(report)
        events.put((time.time(), name, None, "finished", ""))
    except Exception as error:
        events.put((time.time(), name, None, type(error).__name__, str(error)))

threads = [threading.Thread(target=run, args=("T1", job1)),
           threading.Thread(target=run, args=("T2", job2))]
for t in threads: t.start()
for t in threads: t.join()
timeline = sorted(events.queue)              # then show it with st.dataframe in the main thread
```

Each thread must open its **own** transaction (`db.transaction` always opens a new connection, so
that's automatic). Start the second thread a fraction of a second after the first (`time.sleep(0.3)`)
so the order is predictable.

---

## 4. The three cases: what to run and what to expect

Use a **demo row** (pick one ID and note its starting values) and reset it between runs. Run each
case under all four levels. PostgreSQL treats READ UNCOMMITTED exactly as READ COMMITTED, so expect
identical results for those two: **say so in the report**.

**Case 1: concurrent reads (same row, 2+ nodes)**
Two or three transactions read the same row on different servers at the same time.
Expected at every level: all succeed, nobody waits (readers never block readers), all see the same
committed value.

**Case 2: one write while others read**
T1 updates the row and holds its transaction open (sleep at `"logged"`); T2/T3 read the row twice,
before and after T1 commits.

| Level | Reader on the **same server** as the write |
|---|---|
| READ UNCOMMITTED / READ COMMITTED | never sees the uncommitted value (no dirty reads); its *second* read can see the new value (non-repeatable read) |
| REPEATABLE READ / SERIALIZABLE | sees the same value in both reads (its snapshot is fixed at its first query) |

**Across servers:** a reader on the fragment can still see the **old** value for a moment after T1
commits on server 0, at **any** level, until delivery reaches the fragment. Isolation levels work
inside one server; the copies converge right after (replication lag of a primary-copy design). Not a
dirty read: the old value was committed. The final "all copies match" check should still pass.

**Case 3: concurrent writes to the same row**
T1 and T2 both read the row, then both try to update (or delete) it, from different servers.
T2's write **waits** for T1's row lock, then:

| Level | What T2 gets (with `writes.update_row`, i.e. with the before-check) |
|---|---|
| READ UNCOMMITTED / READ COMMITTED | `EditConflict`: PostgreSQL re-checks T2's `WHERE` against the new row, the before-values no longer match, 0 rows change |
| REPEATABLE READ / SERIALIZABLE | `Refused` with sqlstate 40001, "could not serialize access due to concurrent update" |

Either way only T1's change survives, on both copies: **no lost update**.

Optional extra for the report: a "naive" T2 that reads the delay, computes a new one in Python and
runs a plain `UPDATE ... SET delay = %s WHERE stop_rdt_id = %s` inside one transaction. Under READ
COMMITTED this **overwrites T1 (a lost update)**; under REPEATABLE READ and SERIALIZABLE it's refused.
That contrast shows why the app uses the before-check (optimistic concurrency control).

**Record for every run:** case, level, which servers each transaction used, the timeline (time,
transaction, server, step), who succeeded or failed and why, and the final copies on server 0 and
the fragment. Save runs (e.g. to a list in `st.session_state`, or a downloadable CSV) for the report.

---

## 5. Node monitor page

Suggested content, refreshed every ~2 s with `@st.fragment(run_every=2)`:

- **Each server**: up/down (`db.ping_all`), ready or RECOVERING (`access.is_ready`).
- **Replication**: `replication.log_summaries(settings)` (pending and conflicts per log → target),
  plus the **Replay now** button (`replication.replay_all`). Move these from the Connection check page.
- **Active transactions and lock waits** on each server:

  ```sql
  SELECT pid, application_name, state, wait_event_type, now() - xact_start AS running_for, query
  FROM pg_stat_activity
  WHERE datname = 'railway' AND pid <> pg_backend_pid();

  SELECT pid, locktype, mode, granted FROM pg_locks WHERE NOT granted;   -- who is waiting
  ```

  `application_name` shows which app copy opened the connection ("stadvdb-app (from server 1)").
  To see other connections' queries, the `app` login needs one grant, once per server:

  ```bash
  sudo -u postgres psql -c "GRANT pg_read_all_stats TO app;"
  ```

---

## 6. Step 4: crash and recovery demos

Simulate a crash by stopping PostgreSQL on one server, and recover by starting it:

```bash
sudo systemctl stop postgresql      # crash
sudo systemctl start postgresql     # recover
```

| Spec case | How to show it |
|---|---|
| 1. Replication fragment → central fails | stop **server 0**; edit a row on server 1's or 2's page. It commits on the fragment; its entry for server 0 stays `pending`. |
| 2. Central recovers having missed writes | start **server 0**. It shows RECOVERING (reads/writes skip it) until the next replay (≤ 40 s, or Replay now); then `applied`, ready again. |
| 3. Replication central → fragment fails | stop **server 1** (or 2); edit one of its rows. It commits on server 0; the entry for the fragment stays `pending`. |
| 4. Fragment recovers having missed writes | start it again; RECOVERING, then caught up by the replay. |

The replay service logs what happens: `sudo journalctl -u stadvdb-replay -f` ("waiting …",
"server N is back online", "applied N").

---

## 7. For the report: points already worked out

- **Data transparency**: Records never shows servers; the access layer hides fragmentation (which
  fragment holds a row), location (which server answers) and replication (two copies, one result).
- **Replication strategy**: primary-copy replication with server 0 as primary; eager in normal
  operation (delivered before the user is told "saved"), lazy through the replication log while a
  node is down; writes fail over to the fragment's copy while server 0 is unavailable. Not two-phase
  commit: the two commits are separate; the log guarantees the copies converge.
- **Concurrency safeguards**: PostgreSQL row locks; isolation levels; the **before-check** (optimistic
  concurrency control: protects against lost updates *across* transactions, which no isolation level
  can); a 20 s lock timeout; PostgreSQL's deadlock detector within a server; the fixed "server 0
  first" order against deadlocks across servers; the delivery lock plus the `init_`/`fin_` check
  (the before-check for replication, safe to repeat).
- **READ UNCOMMITTED = READ COMMITTED** in PostgreSQL (no dirty reads).
- **Per-server isolation**: the replication-lag observation from Case 2.
- **CAP**: we simulate crashes, not partitions; one failure at a time. Under a partition we'd have to
  choose between consistency (refuse) and availability (serve possibly old data).
- **Known edge**: while server 0 is down, two inserts on different fragments could pick the same new ID;
  the replay would flag it as a conflict.
- **Setup note**: the databases were first created as SQL_ASCII (no encoding); they were rebuilt as
  UTF8, and the load scripts now enforce it.
