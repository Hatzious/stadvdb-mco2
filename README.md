# stadvdb-mco2

A distributed database system running PostgreSQL across 3 Proxmox nodes, built on the
[Dutch railway services dataset](https://duckdb.org/docs/2.0/guides/snippets/dutch_railway_datasets).
The data source URL is in [source.txt](source.txt).

## Workflow: when the repo changes

Changes flow one way: **laptop → GitHub → servers**. Edit and commit on a laptop, push, then update
each server. Servers never commit or push.

**On your laptop**, after committing:

```powershell
git push
```

**On each server** (server 0, 1 and 2), as your regular user:

```bash
cd ~/stadvdb-mco2
git pull
uv sync
sudo bash scripts/deploy-app.sh
```

- `git pull` fetches the new code.
- `uv sync` installs any package changes from `uv.lock`. Harmless when nothing changed, so always run it.
- `sudo bash scripts/deploy-app.sh` reinstalls the server config from `deploy/`, restarts the webapp and
  the replay service so they run the new code, and checks that Streamlit, nginx and the replay service
  all work. If a check fails, the update is broken on that server: fix it before moving on.

Some changes need one extra step on each server:

| If this changed | Also run |
|---|---|
| `scripts/setup-node.sh` | `sudo bash scripts/setup-node.sh` |
| `schema/services.sql` | See [When schema/services.sql changes](#when-schemaservicessql-changes) |
| `schema/replication_log.sql` | See [The replication log](#the-replication-log) |
| `schema/services_indexes.sql` | See [Sort indexes](#sort-indexes) |

## Node setup

Run these on each node's console (Proxmox web GUI → select the container → **Console**).
The commands assume you are logged in as a regular user with `sudo` rights.

### 1. Install git

The setup script lives in this repo, so git has to be installed before the repo can be cloned.

```bash
sudo apt update
sudo apt install -y git
```

### 2. Clone the repo

> **First time only.** Skip this step if the `stadvdb-mco2` folder already exists on the node.

```bash
git clone https://github.com/Hatzious/stadvdb-mco2.git
```

### 3. Go into the repo folder

```bash
cd stadvdb-mco2
```

### 4. Run the setup script

Installs everything the node needs (curl, wget, git, PostgreSQL, SSH server, tmux, nginx) and uv,
then prints the version of each. It is safe to run more than once. Run it with `sudo` from your regular
user, not as root: uv installs per user, and the script installs it for whoever ran `sudo`.

```bash
sudo bash scripts/setup-node.sh
```

### Updating a node later

When the setup script changes, pull the latest version and run it again:

```bash
cd stadvdb-mco2
git pull
sudo bash scripts/setup-node.sh
```
### Running SSH

Enable SSH
```bash
sudo systemctl enable --now ssh
```

### Connect from personal PC (Ask me for password)

#### Hera
```powershell
ssh -p 20160 greateststrongestbbc@103.231.240.136
```

#### Artemis
```powershell
ssh -p 20161 greateststrongestbbc@103.231.240.136
```

#### Athena
```powershell
ssh -p 20162 greateststrongestbbc@103.231.240.136
```

## Loading the dataset

Run these on the node as your regular user, from the repo folder (`cd ~/stadvdb-mco2`).
The dataset is saved in `data/`, which git ignores, so it never gets committed.

### 1. Download the dataset

Downloads the file listed in [source.txt](source.txt) into `data/`. If the download gets cut off,
running it again resumes where it stopped.

```bash
bash scripts/download-data.sh
```

### 2. Load it into PostgreSQL

Creates the `railway` database and the `services` table (from [schema/services.sql](schema/services.sql))
if they don't exist yet, then loads the data. It takes several minutes; run it inside `tmux` so a dropped
SSH connection doesn't stop it.

The database is created with **UTF8** encoding. Without it, PostgreSQL may fall back to `SQL_ASCII`
("no encoding") on a container with no language set, and the webapp would then receive text as raw
bytes. If an existing `railway` database isn't UTF8, the script stops and prints how to drop it.

```bash
bash scripts/load-data.sh
```

The CSV has 17 columns, but `services` keeps only 5. The script loads the whole CSV into a temporary
table first ([schema/incoming.sql](schema/incoming.sql)), copies the 5 kept columns into `services`,
then deletes the temporary table. Rows whose `stop_rdt_id` is already in `services` are skipped,
so running it twice never duplicates data.

### When schema/services.sql changes

The scripts never change an existing table. After editing the schema, drop the table on each node
(this deletes its rows), then run the load script to recreate and fill it:

```bash
sudo -u postgres psql -d railway -c "DROP TABLE services;"
bash scripts/load-data.sh
```

### Wiping and reloading

Empties the `services` table and loads the dataset into it again from scratch. It asks you to type
`yes` first. The table definition (including any primary key) is kept; only the rows are deleted.

```bash
bash scripts/reload-data.sh
```

### Opening PSQL on terminal

```bash
sudo -u postgres psql -d railway
```

## Network setup

Lets the three servers' PostgreSQL databases talk to each other over the internal network, so the
webapp on any server can read from and replicate to the others. Only these three servers are allowed
in, and they must log in with a password. Run the steps on **every** server unless a step says otherwise.

| Server | Container | Internal address | Holds |
|---|---|---|---|
| server 0 | Hera | `10.20.200.160` | All rows (central node) |
| server 1 | Artemis | `10.20.200.161` | Sprinter rows |
| server 2 | Athena | `10.20.200.162` | All other rows |

The ports the university gave us (SSH, HTTP, MySQL) are for reaching the servers **from outside**
through the gateway. Server-to-server traffic uses the internal addresses above and normal ports.

To see a server's internal address:

```bash
hostname -I
```

### 1. Check the servers can reach each other

`ping` doesn't work in these containers (they lack the permission it needs), so test a real port instead.
This tries to connect to SSH (port 22) on another server and closes the connection immediately.
Run it from each server against the other two:

```bash
timeout 3 bash -c '</dev/tcp/10.20.200.161/22' && echo OPEN || echo CLOSED
```

- `OPEN`: the servers can reach each other.
- `CLOSED` immediately: reached the server, but nothing is listening on that port.
- `CLOSED` after 3 seconds: nothing answered; something is blocking the traffic.

### 2. Make PostgreSQL listen on the network

By default PostgreSQL only accepts connections from inside its own server (`localhost`).
This makes it listen on every address of the server. It is still only port 5432.

```bash
echo "listen_addresses = '*'" | sudo tee /etc/postgresql/16/main/conf.d/listen.conf
```

PostgreSQL reads every file in `conf.d/` automatically. If `ls /etc/postgresql` shows a version
other than `16`, use that number in the paths instead.

### 3. Allow only the three servers to connect

`pg_hba.conf` is PostgreSQL's list of who may connect, from where, and how they must log in.
This appends one line per server: that exact address (`/32`), password login (`scram-sha-256`).

```bash
sudo tee -a /etc/postgresql/16/main/pg_hba.conf <<'EOF'

# Project servers (server 0, 1, 2). Password login required.
host  all  all  10.20.200.160/32  scram-sha-256
host  all  all  10.20.200.161/32  scram-sha-256
host  all  all  10.20.200.162/32  scram-sha-256
EOF
```

> Keep the `-a`. Without it, `tee` replaces the whole file and the default local logins stop working.

### 4. Restart PostgreSQL and check it's listening

```bash
sudo systemctl restart postgresql
sudo ss -tlnp | grep 5432
```

You should see `0.0.0.0:5432` and `[::]:5432`. If you see `127.0.0.1:5432` instead, step 2 didn't
take effect: check the file exists with `cat /etc/postgresql/16/main/conf.d/listen.conf`, then restart again.

### 5. Test PostgreSQL from the other servers

Run both from each server against the other two:

```bash
timeout 3 bash -c '</dev/tcp/10.20.200.160/5432' && echo OPEN || echo CLOSED
pg_isready -h 10.20.200.160 -p 5432
```

`pg_isready` goes one step further than the port test: it checks that PostgreSQL itself is answering
and ready. You want `accepting connections`.

### 6. Create the `app` login

The webapp and the replication between servers log in as `app`. Use the **same password on all three
servers**, and share it privately, never in this repo. The `postgres` admin account stays local-only.

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE app WITH LOGIN;
\password app
\q
```

`\password` asks for the password twice and shows nothing as you type.

Test it from another server; it should print the address of the server you logged in to:

```bash
psql -h 10.20.200.160 -U app -d postgres -c "SELECT inet_server_addr();"
```

- `password authentication failed`: wrong password, or a typo when it was set.
- `no pg_hba.conf entry for host`: step 3 is missing on the server you're connecting **to**.

`app` can log in but can't touch the data yet. Access to the `services` table is granted in the next section.

## Loading the fragments

Server 0 holds every row. Servers 1 and 2 each copy their share **from server 0** over the network:

| Server | Fragment | Rule |
|---|---|---|
| server 1 | `sprinter` | `service_type = 'Sprinter'` (~45% of rows) |
| server 2 | `others` | `service_type IS DISTINCT FROM 'Sprinter'` (~55% of rows) |

`IS DISTINCT FROM` (rather than `<>`) also catches rows with an empty type, so every row of server 0
lands in exactly one fragment. Servers 1 and 2 don't need to download the dataset.

### 1. Let `app` use the table on server 0 (server 0, once)

Server 0's table was created before the `app` login existed, so grant access by hand. Tables created
from [schema/services.sql](schema/services.sql) from now on get this automatically.

```bash
sudo -u postgres psql -d railway -c "GRANT SELECT, INSERT, UPDATE, DELETE ON services TO app;"
```

### 2. Copy the fragment (server 1 and server 2)

Run inside `tmux`. It asks for your `sudo` password and for `app`'s password, then creates the
`railway` database (UTF8, as in [Load it into PostgreSQL](#2-load-it-into-postgresql)) and `services`
table if needed and copies the rows.

On server 1:

```bash
bash scripts/load-fragment.sh sprinter
```

On server 2:

```bash
bash scripts/load-fragment.sh others
```

The script compares row counts with server 0 at the end. If the copy fails or the counts don't match,
it empties the table so no half-copied fragment is left. To empty the table and copy again on purpose,
add `--reset`:

```bash
bash scripts/load-fragment.sh sprinter --reset
```

### 3. Check the fragments add up

The two fragments together must equal server 0. Compare these three numbers:

```bash
sudo -u postgres psql -d railway -c "SELECT count(*) FROM services;"
```

Run it on each server: server 1's count plus server 2's count should equal server 0's count.

## The replication log

Every server keeps a `replication_log` table ([schema/replication_log.sql](schema/replication_log.sql)).
Each write adds an entry for the server holding the row's other copy; the app delivers it and marks
it `applied`. Entries for a server that is down stay `pending` until it comes back.

### Create it (all three servers)

From the repo folder, on server 0, 1 and 2:

```bash
cd ~/stadvdb-mco2
sudo -u postgres psql -d railway < schema/replication_log.sql
```

Safe to run again: anything that already exists is skipped. Check it's there:

```bash
sudo -u postgres psql -d railway -c "\d replication_log"
```

### When schema/replication_log.sql changes

Running the file again does **not** change an existing table. The log only holds replication
history, so drop it and create it again (this deletes its entries, see the warning below):

```bash
sudo -u postgres psql -d railway -c "DROP TABLE replication_log;"
sudo -u postgres psql -d railway < schema/replication_log.sql
```

### Emptying it

Resets the log between demo runs. It shows how many entries there are and asks you to type `yes`:

```bash
bash scripts/clear-replication-log.sh
```

Add `--yes` to skip the question. **Warning:** a `pending` entry is a write another server hasn't
received yet. Deleting it means that server never gets it, and the row stays different between the
two servers. The script warns you when pending entries exist.

## Sort indexes

The Records page can sort by arrival time and by delay. To make that fast, each server keeps two
indexes ([schema/services_indexes.sql](schema/services_indexes.sql)). The load scripts build them at the
end of a load. On a server loaded before they existed, build them once (a few minutes on server 0;
the server keeps working meanwhile):

```bash
cd ~/stadvdb-mco2
sudo -u postgres psql -d railway < schema/services_indexes.sql
```

Check they're there (`services_by_arrival_time` and `services_by_arrival_delay`, under "Indexes"):

```bash
sudo -u postgres psql -d railway -c "\d services"
```

If a build was interrupted, `\d services` marks that index `INVALID`. Drop it and run the file again:

```bash
sudo -u postgres psql -d railway -c "DROP INDEX services_by_arrival_time;"   # or services_by_arrival_delay
sudo -u postgres psql -d railway < schema/services_indexes.sql
```

## Python environment

The webapp's packages are listed in [pyproject.toml](pyproject.toml), with exact versions locked in
`uv.lock`; the Python version (3.12) is pinned in `.python-version`. Each machine (laptops and all
three servers) installs them into its own `.venv` folder, which git ignores. After every `git pull`:

```bash
uv sync
```

To add a package, run `uv add <package>` on a laptop and commit `pyproject.toml` and `uv.lock`.
Never edit `uv.lock` by hand.

## Configuration

Each machine (every laptop and each server) keeps its own settings in `.streamlit/secrets.toml`:
the database password, where each server's database is, and which server this copy of the app is.
Git ignores that file, so the password never reaches GitHub. Start from the committed template,
[.streamlit/secrets.example.toml](.streamlit/secrets.example.toml).

### On each server

```bash
cd ~/stadvdb-mco2
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
chmod 600 .streamlit/secrets.toml
nano .streamlit/secrets.toml
```

`chmod 600` makes the file readable by your user only. In the file, set:

- `server_id`: `0` on server 0, `1` on server 1, `2` on server 2
- `password`: the `app` password

Leave the three server addresses as they are. Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`,
and run `sudo bash scripts/deploy-app.sh` so the app picks up the settings.

### On a laptop

```powershell
Copy-Item .streamlit\secrets.example.toml .streamlit\secrets.toml
```

Open `.streamlit\secrets.toml` in your editor and set:

- `server_id`: the server you want the app to act as while testing (`0`, `1` or `2`)
- `password`: the `app` password
- the three servers, to go through the [SSH tunnel](#development-access-ssh-tunnel):

| Section | `host` | `port` |
|---|---|---|
| `[servers.0]` | `localhost` | `15430` |
| `[servers.1]` | `localhost` | `15431` |
| `[servers.2]` | `localhost` | `15432` |

The laptop has no data of its own: through the tunnel, the app reads and writes the real databases on
the servers. Test with demo rows, and tell your groupmates before running anything that changes data.

## Running the webapp

The webapp's code lives in the [app/](app) folder. The entry file is [app/app.py](app/app.py): it holds
the sidebar (the **isolation level** every page uses) and the page menu. The pages are in
[app/views/](app/views):

| Page | What it does |
|---|---|
| **Records** | Browse, filter, sort, edit, delete and add records. Never shows which server answered. |
| **Connection check** | Which server this copy of the app is, whether it reaches all three databases, and the replication state with a **Replay now** button. If `secrets.toml` is missing or wrong, it says what to fix. |

[app/settings.py](app/settings.py) reads this machine's `.streamlit/secrets.toml`. Shared Streamlit
settings are in [.streamlit/config.toml](.streamlit/config.toml).

### On a laptop (development)

Open the [SSH tunnel](#development-access-ssh-tunnel) first, then from the repo folder:

```powershell
uv run streamlit run app/app.py
```

Then open http://localhost:8501. Stop it with `Ctrl+C`. Always start it from the repo folder, so
Streamlit finds the `.streamlit/` settings.

### On a server (deployment)

```
browser -> 103.231.240.136:1016x (gateway) -> nginx on port 80 -> Streamlit on 127.0.0.1:8501
```

Streamlit only listens on `127.0.0.1`, so the only way in from outside is through nginx. It runs as a
systemd service called `stadvdb-app` under your regular user, so it keeps running after you log out and
starts again after a reboot.

On each server, from the repo folder, as your regular user:

```bash
git pull
uv sync
sudo bash scripts/deploy-app.sh
```

The script installs two services, the webapp ([deploy/stadvdb-app.service](deploy/stadvdb-app.service))
and the replay service ([deploy/stadvdb-replay.service](deploy/stadvdb-replay.service)), plus the nginx
site ([deploy/nginx-stadvdb.conf](deploy/nginx-stadvdb.conf)). It removes nginx's default "Welcome" page
and checks that Streamlit, nginx and the replay service all work. Running it again redeploys. Then open:

| Server | URL |
|---|---|
| server 0 | http://103.231.240.136:10160 |
| server 1 | http://103.231.240.136:10161 |
| server 2 | http://103.231.240.136:10162 |

Useful commands on a server:

```bash
sudo journalctl -u stadvdb-app -f       # app log, live (Ctrl+C to stop watching)
sudo systemctl status stadvdb-app       # is it running?
sudo journalctl -u stadvdb-replay -f    # replay log, live: what was delivered, and what's waiting
```

### The replay service

`stadvdb-replay` runs [app/replay_worker.py](app/replay_worker.py). Every 40 seconds it delivers this
server's `replication_log` to the other two servers, so a server that was down catches up shortly after
it comes back, even if nobody has a page open. It only writes to its log when something happened. The
connection check page also has a **Replay now** button that delivers every log at once.

The interval is `REPLAY_INTERVAL_SECONDS` in [app/replication.py](app/replication.py). To change it,
edit it on a laptop and follow the [Workflow](#workflow-when-the-repo-changes): the deploy script
restarts the service with the new value.

### Restarting the app

The webapp runs as a systemd service called `stadvdb-app`, so it keeps running after you log out and
starts by itself after a reboot. To restart it, always use the deploy script, so every restart ends with
the health checks:

```bash
sudo bash scripts/deploy-app.sh
```

Restart it:

- after `git pull` and `uv sync` (see [Workflow](#workflow-when-the-repo-changes))
- after changing `.streamlit/secrets.toml` or `.streamlit/config.toml` on that server
- if the page stops responding

Other controls:

| Command | What it does |
|---|---|
| `sudo systemctl stop stadvdb-app` | Turn the app off. It comes back after a reboot. |
| `sudo systemctl start stadvdb-app` | Turn it back on |
| `sudo systemctl disable --now stadvdb-app` | Turn it off and keep it off after reboots |
| `sudo systemctl enable --now stadvdb-app` | Undo `disable`: on now and at every boot |
| `sudo systemctl status stadvdb-app` | Is it running? Shows the last few log lines too |

The same commands work for the replay service: replace `stadvdb-app` with `stadvdb-replay`.

## Development access (SSH tunnel)

The databases are only reachable on the servers' internal network; the gateway doesn't forward port 5432.
To use them from your laptop while developing, open an SSH tunnel through server 0. Run this in
PowerShell on your laptop and leave the window open; the tunnel lasts only while it stays open.

```powershell
ssh -N -p 20160 -L 15430:10.20.200.160:5432 -L 15431:10.20.200.161:5432 -L 15432:10.20.200.162:5432 <username>@103.231.240.136
```

| On your laptop | Leads to |
|---|---|
| `localhost:15430` | server 0's PostgreSQL (`10.20.200.160:5432`) |
| `localhost:15431` | server 1's PostgreSQL (`10.20.200.161:5432`) |
| `localhost:15432` | server 2's PostgreSQL (`10.20.200.162:5432`) |

- `-L laptop-port:target-address:target-port` opens `laptop-port` on your laptop. Anything that connects
  to it travels inside the SSH connection, and server 0 passes it on to `target-address:target-port`.
- `-N` means "tunnel only, no shell", so the window shows nothing after you log in. That's expected.
  Leave out `-N` if you also want a shell on server 0 in the same window.
- One SSH connection reaches all three databases, because server 0 can reach the others internally.
  Servers 1 and 2 see the connection coming from server 0's address, which `pg_hba.conf` allows.
- The 154xx ports are chosen so they don't clash with a PostgreSQL installed on your laptop (5432).

Test it from a second PowerShell window (needs `psql` on your laptop), logging in as `app`:

```powershell
psql -h localhost -p 15431 -U app -d railway -c "SELECT inet_server_addr();"
```

It should print `10.20.200.161`: you're on server 1's database.

The tunnel is for development only. The deployed app runs on the servers themselves and connects to
the internal addresses directly.