# stadvdb-mco2

A distributed database system running PostgreSQL across 3 Proxmox nodes, built on the
[Dutch railway services dataset](https://duckdb.org/docs/2.0/guides/snippets/dutch_railway_datasets).
The data source URL is in [source.txt](source.txt).

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

Installs everything the node needs (curl, wget, git, PostgreSQL). It is safe to run more than once.

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
`railway` database and `services` table if needed and copies the rows.

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