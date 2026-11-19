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
if they don't exist yet, then loads every row. It takes a few minutes; run it inside `tmux` so a dropped
SSH connection doesn't stop it.

```bash
bash scripts/load-data.sh
```

If the table already has rows, the script loads only the rows that aren't there yet and skips the rest,
so data never gets duplicated. Skipping needs a primary key on the table; without one, the script stops
and loads nothing.

### Wiping and reloading

Empties the `services` table and loads the dataset into it again from scratch. It asks you to type
`yes` first. The table definition (including any primary key) is kept; only the rows are deleted.

```bash
bash scripts/reload-data.sh
```