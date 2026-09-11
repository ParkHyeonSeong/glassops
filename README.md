# GlassOps

GlassOps is a web dashboard for watching Linux servers from a browser: CPU, memory, disk,
GPU, network, processes, logs and Docker containers, laid out like a macOS desktop with
windows and a dock. It runs as a single Docker container. To watch more than one machine,
run the small agent container on each extra host and pick the host from a dropdown in the
dashboard.

## Quick Start

```bash
git clone https://github.com/ParkHyeonSeong/glassops.git
cd glassops
cp .env.example .env      # required — Docker Compose refuses to start without it
make up
```

Open **http://localhost:7440**.

**First login.** On the first start GlassOps creates one admin account,
`admin@glassops.local`, with a random one-time password. The password is never printed to
the logs; it is written to a file inside the data volume, which you read like this:

```bash
docker compose exec glassops cat /app/data/initial_admin_password
```

Log in with it and you will be asked to choose a new password right away. The file is
deleted once the password has been changed.

> Prefer to pick the password yourself? Set `GLASSOPS_ADMIN_PASSWORD` in `.env` before the
> first start. If the password file cannot be written, startup fails with an error rather
> than falling back to logging the password — fix the data-directory permissions or set
> `GLASSOPS_ADMIN_PASSWORD`.

## What's Inside

| App | What it does |
|-----|--------------|
| System Monitor | CPU, memory and disk gauges plus history charts (Live / 5m / 1h / 6h / 24h / 7d) |
| GPU Monitor | NVIDIA GPUs: utilization, VRAM, temperature, power, clocks, fan speed, per-process VRAM |
| Docker | Containers (start / stop / restart), live and historical container logs, per-container CPU/memory history, plus Images, Volumes and Networks |
| Network | Upload/download rates, interface details, active connections. An **Audit** tab shows connection events and traffic rollups when `GLASSOPS_ENABLE_NET_AUDIT` is on |
| Process Viewer | Sortable process table with CPU/memory bars, search, kill with confirmation |
| Logs | Host system logs and container logs, with search and auto-refresh |
| Terminal | Web terminal (xterm.js) into the selected host; idle sessions are closed after 30 minutes |
| Settings | Profile, alert thresholds, runtime server toggles, email alerts, wallpaper |
| Users | Accounts and roles (`admin` / `user`), per-user host account mapping for the terminal, 2FA status |

Docker, Users, Terminal and the Network Audit tab are admin-only. Every app follows the host
selected in the menu bar.

## How It Works

```
┌─────────────────────────────────────────┐         ┌─────────────────────────────┐
│  Dashboard host (:7440)                 │         │  Remote host                │
│                                         │         │                             │
│  nginx ─── Frontend (React static)      │         │                             │
│    │                                    │         │                             │
│    ├─/api/  ─ Backend (FastAPI)         │         │                             │
│    └─/ws/   ─ WebSocket relay  ◄────────┼─ ws ──► │  Agent (psutil + Docker SDK)│
│                                         │ metrics │   • pushes metrics          │
│  Local agent (built in, via supervisord)│ + RPC   │   • answers RPC requests    │
│                                         │         │     (logs / actions / shell)│
└─────────────────────────────────────────┘         └─────────────────────────────┘
```

An **agent** is the part that actually reads numbers off a machine (psutil for CPU, memory
and processes, the Docker SDK for containers, NVML for GPUs). The dashboard container has one
built in for the machine it runs on — that is the `local` agent. Each additional host runs
the agent-only container, which connects *back* to the dashboard over one WebSocket carrying
two kinds of traffic:

- **Metrics push** (agent → dashboard): system / GPU / Docker / network / process snapshots
  every `GLASSOPS_COLLECT_INTERVAL` seconds.
- **RPC** (dashboard → agent → dashboard): when you restart a container, stream its logs,
  kill a process or open a terminal, the request travels to the selected agent and the answer
  comes back over the same connection. The local agent skips this hop.

Metrics are stored in a SQLite database in the data volume and are collected whether or not
anyone has the dashboard open.

## Requirements

- A Linux host with Docker Engine and Docker Compose v2. The host-level features (process
  list, host logs, web terminal) rely on Linux namespaces (`pid: host`, `nsenter`).
- For NVIDIA GPU monitoring: the NVIDIA driver on the host and `GLASSOPS_ENABLE_GPU=true`.
  The container picks up `libnvidia-ml` from the host at start.
- macOS with Docker Desktop is fine for trying the UI, but Docker runs in a Linux VM there:
  Process Viewer and Terminal show the VM, not macOS, and the `make agent-*` targets expect
  Linux tools.

## Configuration

`.env` is read by Docker Compose when the container starts. Start from the example file and
change what you need:

```bash
cp .env.example .env
```

**Basics**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_PORT` | `7440` | Web UI port |
| `GLASSOPS_BIND` | `127.0.0.1` | Address the port is published on. The default is reachable from this machine only — right for single-host use and behind a reverse proxy. Remote agents need to reach the port directly: set the host's LAN IP (e.g. `10.0.0.9`), or `0.0.0.0` plus a firewall rule |
| `GLASSOPS_SECRET_KEY` | *(generated)* | Master secret: signs sessions, derives the agent key, encrypts SMTP passwords. **Set it explicitly in production** (`openssl rand -hex 32`). If empty, one is generated and stored at `<data>/secret.key`. Weak or placeholder values are rejected at startup |
| `GLASSOPS_ADMIN_EMAIL` | `admin@glassops.local` | Email of the first admin account |
| `GLASSOPS_ADMIN_PASSWORD` | *(random)* | Password of the first admin. If unset, a one-time password is written to `<data>/initial_admin_password` (mode 0600) and must be changed at first login |
| `GLASSOPS_DB_PATH` | `/app/data/glassops.db` | SQLite database path inside the container |

**Collection**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_COLLECT_INTERVAL` | `1` | Seconds between metric snapshots (1–60) |
| `GLASSOPS_ENABLE_DOCKER` | `true` | Collect container metrics and enable the Docker app |
| `GLASSOPS_ENABLE_GPU` | `false` | Collect NVIDIA GPU metrics (needs the host driver) |
| `GLASSOPS_LOCAL_AGENT_ID` | `local` | Id of the built-in agent. Requests for this id skip RPC and use the local Docker socket directly |
| `GLASSOPS_AGENT_ID` | `local` | Remote agents only (`agent.env`): a unique name per host. Inside the dashboard container it is always overridden with `GLASSOPS_LOCAL_AGENT_ID` |
| `GLASSOPS_AGENT_KEY` | *(derived)* | Remote agents only: the key a remote agent presents. Get it from the dashboard host with `docker compose exec glassops python -m app.secret_bootstrap agent`. The built-in agent derives it itself |
| `GLASSOPS_RPC_TIMEOUT` | `30` | Seconds to wait for an agent to answer an RPC (logs, actions, …) |

**Terminal**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_TERMINAL_USER` | *(unset)* | Host user the local terminal logs in as when the dashboard user has no mapping in the Users app |
| `GLASSOPS_ALLOW_LOGIN_PROMPT` | `false` | With no terminal user configured, show the host `login` prompt instead of refusing. Lets an admin log in as **any** host account, root included — off by default |

**Network access, proxies, browsers**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_ALLOWED_IPS` | *(all)* | Comma-separated CIDR allowlist of client addresses. Checked against the real client IP — behind a proxy, also set `GLASSOPS_TRUSTED_PROXIES` |
| `GLASSOPS_TRUSTED_PROXIES` | `127.0.0.1,::1` | CIDRs of reverse proxies whose forwarded headers are believed. Used by both the backend and nginx `real_ip`, so the allowlist and per-IP rate limits see the true client |
| `GLASSOPS_FORCE_SECURE_COOKIES` | `false` | Mark auth cookies `Secure` when TLS is terminated by an upstream proxy (see Production Deployment) |
| `GLASSOPS_ALLOWED_ORIGINS` | *(same host)* | Browser origins accepted by the WebSocket origin check and the CSRF check on state-changing requests, e.g. `https://ops.example.com`. Empty = the `Origin` must match the request `Host` |
| `GLASSOPS_CORS_ORIGINS` | `http://localhost:3000,http://localhost:3300` | Origins echoed in CORS headers — only needed if the frontend is served from a different origin than the API |

**Email and network audit**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_SMTP_ALLOWED_HOSTS` | *(any safe host)* | Restrict which SMTP relay hosts may be configured (see Email Alerts) |
| `GLASSOPS_ENABLE_NET_AUDIT` | `false` | Record connection events and per-minute traffic rollups per host (metadata only; admin-only, audit-logged) |
| `GLASSOPS_NET_AUDIT_MAX_EVENTS` | `200` | Max connection events an agent sends per snapshot (the rest are dropped and counted) |
| `GLASSOPS_NET_AUDIT_TOP_TALKERS` | `20` | Remote peers kept per rollup |
| `GLASSOPS_NET_AUDIT_EVENT_DAYS` | `7` | Retention (days) of raw connection events |
| `GLASSOPS_NET_AUDIT_ROLLUP_DAYS` | `30` | Retention (days) of traffic rollups |

> Five of these can be changed while running, from **Settings > Server** (admin):
> `GLASSOPS_ENABLE_GPU`, `GLASSOPS_ENABLE_DOCKER`, `GLASSOPS_COLLECT_INTERVAL`,
> `GLASSOPS_TERMINAL_USER`, `GLASSOPS_ALLOWED_IPS`. Everything else is read from `.env`
> when the container is created — run `make up` after editing it (a plain restart keeps the
> old environment).

## Make Commands

Dashboard host:

```bash
make up          # Build if needed, then start (also applies .env changes)
make down        # Stop
make restart     # Restart the container (does not re-read .env)
make logs        # Follow logs
make status      # Container status and /health
make shell       # Shell inside the container
make build       # Build the image only
make prod        # Rebuild from scratch (no cache) and start
make update      # git pull, rebuild without cache, start
make dev         # Start with backend/agent source mounted (see CONTRIBUTING.md)
make dev-down    # Stop the dev container
make clean       # Stop AND delete the data volume — accounts, settings and history are gone
make help        # List all targets
```

Remote host (agent only — no UI, no database):

```bash
make agent-up        # Start the agent (no GPU)
make agent-up-gpu    # Start the agent with NVIDIA GPU access
make agent-down      # Stop the agent
make agent-logs      # Follow agent logs
```

The agent targets read `agent.env` (copy `agent.env.example`) and detect the host's `docker`
group id so the agent can use `/var/run/docker.sock`.

The quality gates (`make quality` and friends) are described in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Metrics History

GlassOps keeps **7 days** of metrics, thinning older data automatically:

| Age | Resolution | Stored in |
|-----|------------|-----------|
| Last hour | 1 second (raw) | `metrics` |
| 1 hour – 24 hours | 1-minute average | `metrics_downsampled` |
| 1 day – 7 days | 5-minute average | `metrics_downsampled` |

## Email Alerts

Server-side alerts, sent even when nobody is logged in. Configure them in
**Settings > Email** (admin only).

### Connection

| Field | Notes |
|---|---|
| SMTP Host | Hostname only — no `smtp://` scheme and no `:port` suffix. Surrounding whitespace is stripped. |
| Port | One of `25`, `465`, `587`, `2525`. Anything else is refused. |
| Security | `STARTTLS` (587), `Implicit TLS` (465), or `None` (25 or 2525). The port hint is advisory — any allowed port works with any mode. |
| Username | The SMTP **login identifier**; it does not have to be an email address. Leave it blank for an unauthenticated relay. Username and password are all-or-nothing: set both or neither, or the save is refused. |
| Password | Encrypted at rest with Fernet, keyed off a subkey derived from `GLASSOPS_SECRET_KEY`. |
| From Email | Required, unless the username is itself a valid email address, in which case it is used. Without a usable sender the save is refused — an empty From produces the `MAIL FROM:<>` null reverse-path that most relays drop. |
| To Email | Where alerts are delivered. |

`GLASSOPS_SMTP_ALLOWED_HOSTS` (comma-separated) restricts which relay hosts may be
configured. When it is set, only those exact hostnames are accepted **and the
IP-resolution checks are skipped for them** — an allowlisted host is fully trusted, so list
only hosts you vouch for. When it is empty, any host that passes the SSRF checks is allowed:
loopback, link-local (including the `169.254.169.254` cloud metadata address), unspecified,
multicast and reserved addresses are blocked, while RFC1918 private ranges are deliberately
allowed so an internal corporate relay works.

### Email critical thresholds

`CPU critical`, `Memory critical` and `Disk critical` (0–100) live on the server and decide
when an email goes out. They are **separate** from the in-browser thresholds under
**Settings > Alerts**, which only drive the desktop toasts and the System Monitor banner and
feed. Both use a `>=` comparison, so the configured value itself fires.

Only aggregate CPU (`cpu.percent_total`) is evaluated. A single pegged core never triggers
an email — the per-core numbers are a diagnostic display on the Cores tab.

### Saving and testing

**Save & Send Test** saves the form, confirms the save succeeded, and only then sends
through the configuration it just stored. If the save fails, the backend's reason is shown
and no email is sent.

A success message means **the SMTP server accepted the message** — not that it reached the
inbox. It can still bounce, be filtered, or be dropped downstream; confirm in the recipient
mailbox or the provider's delivery log. For an end-to-end check against a real SMTP sink,
see [scripts/smtp-sink-check.md](scripts/smtp-sink-check.md).

The password field shows `********` for a stored credential. Posting that value back keeps
the existing password; typing a new one replaces it. If `GLASSOPS_SECRET_KEY` changes, the
stored ciphertext can no longer be decrypted — the tab says so, sending is blocked, and the
warning clears only once a new password is actually saved. Removing a stored credential
entirely is an API-only operation (`clear_password`); the UI has no control for it.

### Cooldown

After a successful send, further alerts for **that agent** are suppressed for 5 minutes. The
cooldown is per agent, not per resource: while a CPU alert is cooling down, a new disk alert
on the same agent waits too.

A failed send does **not** start that 5-minute cooldown — it backs off for 1 minute instead,
so a transient relay outage delays the next alert by a minute rather than five, while a
persistently dead relay is not retried once per collection tick. The manual **Save & Send
Test** bypasses both, so an admin can always retry immediately.

> **Never commit real SMTP credentials.** There is no environment variable for the SMTP
> username or password — they are set through **Settings > Email** or the admin API and
> stored encrypted. They belong in neither the repository nor a compose file checked into
> it. (`GLASSOPS_SMTP_ALLOWED_HOSTS` is not a credential and is safe to commit.)

## Host Monitoring

GlassOps monitors the **host machine**, not just its own container:

- `pid: host` — sees all host processes
- `/var/log` mounted — reads host system logs
- `/proc` mounted — collects host CPU/memory/disk metrics
- Docker socket — manages host Docker containers
- `nsenter` — the terminal enters the host's namespaces (Linux only)

## Multi-Host Monitoring

By default GlassOps monitors the server it is installed on. To add more hosts, run the
agent-only container on each one; the dashboard shows them all through the host dropdown in
the menu bar.

### 1. Open the backend port to the LAN

On the dashboard host, make port `7440` reachable from the network the remote agents live
on: either set `GLASSOPS_BIND` in `.env` (e.g. `GLASSOPS_BIND=10.0.0.9`) or publish on
`0.0.0.0` and restrict at the firewall, then `make up`.

> The default `127.0.0.1` binding is correct for single-host installs and reverse-proxy
> setups. Remote agents need direct reachability, not a reverse proxy.

### 2. Install the agent on each remote host

```bash
git clone https://github.com/ParkHyeonSeong/glassops.git
cd glassops
cp agent.env.example agent.env
```

Edit `agent.env`:

```env
GLASSOPS_AGENT_ID=dev10                              # unique per host
# Backend's derived agent key — get it from the dashboard host with:
#   docker compose exec glassops python -m app.secret_bootstrap agent
GLASSOPS_AGENT_KEY=<backend derived agent key>
# Use wss:// (TLS). Plaintext ws:// exposes the agent key + all RPC (shell/exec)
# to the network; terminate TLS at a reverse proxy or the dashboard host.
GLASSOPS_SERVER_URL=wss://<dashboard-host>/ws/agent
# GLASSOPS_AGENT_CA=/path/to/ca.pem      # only for a self-signed / private CA
# GLASSOPS_REQUIRE_AGENT_TLS=true        # refuse to start on plaintext remote
GLASSOPS_ENABLE_DOCKER=true
GLASSOPS_ENABLE_GPU=true                             # set false if no NVIDIA GPU
```

> **Remote agents must use `wss://`.** The connection carries the agent key and RPC
> commands (including a shell on the agent host); over plaintext `ws://` anyone on the
> network path can capture the key and inject commands. Put a TLS reverse proxy (Caddy,
> nginx or Traefik with Let's Encrypt) in front of the dashboard, or use a self-signed
> certificate and set `GLASSOPS_AGENT_CA` on each agent.

Then start the agent:

```bash
make agent-up-gpu      # NVIDIA GPU host
# or
make agent-up          # CPU/Docker only
make agent-logs        # follow its logs
```

### 3. Switch hosts in the UI

The menu bar shows a host dropdown once more than one agent is connected. Selecting a host
scopes every panel to it: System Monitor, GPU, Docker (including live log streaming), Logs,
Process Viewer and Terminal all follow the selection.

What works across hosts:

- Real-time metrics (CPU, memory, disk, GPU, network, processes, container list)
- Container start / stop / restart, container detail, images, volumes, networks
- Container log streaming (live tail with autoscroll-follow, plus historical date-range queries)
- System log viewer (host log files are mounted into the agent)
- Process kill (subject to the agent's process privileges)
- Terminal — the host user comes from the dashboard user's mapping for that host in the
  Users app (the `GLASSOPS_TERMINAL_USER` fallback applies to the local agent only)

## Production Deployment

### Reverse proxy (nginx)

GlassOps binds to `127.0.0.1` by default — **a reverse proxy is required for access from
outside the machine**. Set `GLASSOPS_BIND` to a LAN IP only for direct agent connectivity,
not for end-user traffic.

```nginx
server {
    server_name ops.example.com;

    location / {
        proxy_pass http://127.0.0.1:7440;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Required — without this, real-time metrics and the terminal do not work
    location /ws/ {
        proxy_pass http://127.0.0.1:7440;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}
```

> Without the `/ws/` block: System Monitor stays at "Connecting...", Terminal shows
> "Disconnected".

### HTTPS

Over HTTPS, auth tokens are kept in httpOnly cookies instead of `sessionStorage`, which
keeps them out of reach of page scripts:

```bash
sudo certbot --nginx -d ops.example.com
```

> **Terminating TLS at an upstream proxy?** The bundled edge speaks plain HTTP internally
> and deliberately does **not** trust a forwarded `X-Forwarded-Proto` from it (that header
> is client-spoofable at a loopback edge). So when an external proxy terminates TLS, set
> `GLASSOPS_FORCE_SECURE_COOKIES=true` so auth cookies still get the `Secure` flag.

### IP restriction

**Option A — in your reverse proxy** (recommended):
```nginx
allow 10.0.0.0/8;
allow 192.168.0.0/16;
deny all;
```

**Option B — in GlassOps**, via Settings > Server or `.env`:
```env
GLASSOPS_ALLOWED_IPS=10.0.0.0/8,192.168.0.0/16
```

> **Behind a reverse proxy?** GlassOps matches the real client IP. If you front it with an
> LB/TLS proxy, also set `GLASSOPS_TRUSTED_PROXIES` to that proxy's CIDR — otherwise every
> request looks like it came from the proxy and the allowlist (and per-IP rate limits)
> misfire. The same list is honored by nginx `real_ip` and the backend: one knob, not two.

### Terminal user

The web terminal is admin-only and runs as a host user, resolved in this order:

1. **Per-user host mappings** in the **Users** app give each dashboard user a specific host
   account per host. This is the only option for remote hosts.
2. `GLASSOPS_TERMINAL_USER=ubuntu` — one host user for the local terminal, used when the
   dashboard user has no mapping.
3. `GLASSOPS_ALLOW_LOGIN_PROMPT=true` opts into a host `login` prompt, which lets an admin
   authenticate as **any** host account, including root. **Off by default** — with none of
   the above configured, the terminal is refused rather than exposing a login prompt.

The host account's password is still required: GlassOps login + host password.

### Secret key

Set `GLASSOPS_SECRET_KEY` explicitly before deploying:

```bash
GLASSOPS_SECRET_KEY=$(openssl rand -hex 32)
```

It signs sessions, derives the agent key that remote agents present, and encrypts stored
SMTP passwords. If you leave it empty, GlassOps generates one and keeps it at
`<data>/secret.key` — fine for a single host, but back that file up with the data volume.
Changing or losing the key logs everyone out, disconnects remote agents until their key is
updated, and makes stored SMTP passwords undecryptable.

## Operations

### Health and readiness

Two unauthenticated endpoints, both served on the web port:

| Endpoint | Answers | Meaning |
|---|---|---|
| `GET /health` | `200 {"status":"ok"}` | The process is up. Says nothing about storage |
| `GET /ready` | `200` or `503` | `200` while the database is accepting writes; `503` once it is not (fail-stop, shutting down, or an operation whose outcome could not be confirmed). Runs no SQL, so it still answers when the database is stuck |

In plain terms: `/health` says "alive", `/ready` says "actually saving data". The Compose
healthcheck polls `/ready`. A `503` body names the condition (`database`) and whether
`restart_required` is true — in which case the process will not recover on its own: read
`make logs` for the reason (it is logged, not served) and restart the container.

### Backup and restore

Everything GlassOps remembers — accounts and passwords, host mappings, alert and email
settings, the audit log and the last 7 days of metrics — lives in one place: the `/app/data`
directory inside the container, which Docker keeps in the `glassops_data` volume. If
`GLASSOPS_SECRET_KEY` is empty in `.env`, the master secret (`secret.key`) is in there too.
`.env` itself lives on the host next to the compose file and is **not** in the volume.

Back up with the container **stopped**. The database is written to continuously while it
runs, and a copy taken mid-write can be inconsistent:

```bash
docker compose down
docker compose run --rm --no-deps --entrypoint "" -v "$PWD:/backup" glassops \
  tar czf /backup/glassops-data.tgz -C /app/data .
make up
```

Keep `glassops-data.tgz` together with `.env`, and treat both like a password file: between
them they hold the master secret and every account.

To restore — on the same machine, or on a new one after `git clone`, copying the saved
`.env` into place and `make build`:

```bash
docker compose down
docker compose run --rm --no-deps --entrypoint "" -v "$PWD:/backup" glassops \
  sh -c 'rm -rf /app/data/* && tar xzf /backup/glassops-data.tgz -C /app/data'
make up
```

This replaces whatever is in the volume with the backup. Use the same `.env` the backup was
taken with: a different `GLASSOPS_SECRET_KEY` would log everyone out, break stored SMTP
passwords and disconnect remote agents. This is also how you move GlassOps to another
server — the transfer tool two sections below is only for the old-schema upgrade.

### Updating

```bash
make update      # git pull + rebuild without cache + start
```

The data volume is kept. If your database was created before September 2026, read the next
section first.

### Upgrading a database created before September 2026

The database layout changed in September 2026 (metric aggregation tables, one database
boundary). Monitoring history from before that point is **not** carried over — only your
accounts, their host mappings, alert/email settings, revoked tokens, the audit log and the
master secret are. A small tool, `app.account_state_transfer`, moves exactly that into a
fresh database. It is one-way and one-time: it refuses a source database made by the
current build.

The steps, in order. The commands run the tool inside a throwaway container of the new
image because the data volume is normally reachable only as root; from a checkout on the
host the same tool is `cd backend && python3 -m app.account_state_transfer …` (standard
library only).

1. Fetch and build the new version, but do not start it yet:

   ```bash
   git pull && make build
   ```

2. Export **while the old version is still running** — it takes a read-only snapshot and
   changes nothing:

   ```bash
   mkdir -p transfer
   docker compose run --rm --no-deps --entrypoint "" -v "$PWD/transfer:/transfer" glassops \
     python3 -m app.account_state_transfer export \
       --source-db /app/data/glassops.db --bundle /transfer/bundle
   ```

   The bundle contains the master secret in plain text — treat the directory like a
   password.

   > If your `.env` sets `GLASSOPS_SECRET_KEY`, there is no `secret.key` file and the export
   > refuses. Write the same value to a file and point the tool at its directory:
   > `mkdir -p transfer/keydir && grep '^GLASSOPS_SECRET_KEY=' .env | cut -d= -f2- | tr -d '\n' > transfer/keydir/secret.key && chmod 600 transfer/keydir/secret.key`,
   > then add `--data-dir /transfer/keydir` to the export command. Keep the same
   > `GLASSOPS_SECRET_KEY` in the new `.env`.

3. Stop the old version. Keep the volume — do **not** use `make clean`:

   ```bash
   docker compose down
   ```

4. Move the old database aside, inside the volume:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" glassops sh -c \
     'mkdir -p /app/data/old && for f in glassops.db glassops.db-wal glassops.db-shm; do [ -e /app/data/$f ] && mv /app/data/$f /app/data/old/; done; ls -l /app/data/old'
   ```

5. Create the fresh database with the new build — **without starting the server**. If you
   simply started it, its built-in agent would begin storing metrics within seconds and the
   restore would refuse the target as not fresh:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" glassops python3 -c '
   import asyncio
   from app.database import init_db, close_db
   async def main():
       await init_db()
       print(await close_db())
   asyncio.run(main())'
   ```

   It prints `CloseVerdict.CLOSED` and a note about a one-time admin password; ignore the
   note — the restore removes that placeholder account.

6. Restore:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" -v "$PWD/transfer:/transfer" glassops \
     python3 -m app.account_state_transfer restore \
       --bundle /transfer/bundle --target-db /app/data/glassops.db --run-dir /transfer/run-1
   ```

   Success is exit `0`, a JSON report on stdout and the file `transfer/run-1/COMPLETE`. Do
   not start the new version before that file exists. Exit `2` is a refusal with the reason
   on stderr; the database is left fresh, and if the message says so you can re-run with the
   same bundle and a **new** `--run-dir`. Exit `1` is an unexpected error — inspect before
   doing anything else.

7. Start the new version and log in with your existing accounts:

   ```bash
   make up
   ```

   Afterwards delete `transfer/` (it holds the master secret). Once you are satisfied,
   remove the old files as well — `docker compose exec glassops rm -rf /app/data/old` —
   they are the only way back.

The module docstring of `backend/app/account_state_transfer.py` is the full contract.

### Recovering a runaway WAL

SQLite keeps recent writes in `glassops.db-wal` and periodically folds them back into the
main file (a "checkpoint"). If checkpoints stall — typically a connection held open for a
very long time — the WAL keeps growing, into the hundreds of gigabytes if left alone, while
the database itself stays consistent. Folding it back safely needs a moment when nothing can
write: the containers stopped, and nothing allowed to restart them meanwhile.

`app.wal_recovery_cli` runs that procedure in a fixed order and stops at the first refusal:

| Stage | What happens | Exit code on refusal |
|---|---|---|
| fence | Records the restart policy of every container in `--scope`, disables them, stops the containers gracefully (`docker stop --timeout=-1`, never a kill) and writes a manifest | 10 |
| claim | Takes the one-shot lease the manifest issues | 11 |
| recovery | Backs up the database, verifies the backup, checkpoints the WAL. `--sentinel` names a row that must survive; `--probe` tables are compared before and after | 12 |
| terminal | Confirms the run is recorded as COMPLETED | 13 |
| release | Puts the restart policies back. The containers stay stopped — you start them | 14 |

Exit `0` is success; `2` means bad arguments, and nothing on the host was touched. Two
things to know before running it:

- It requires a **signed acknowledgement** (`--host-id`, `--ack-file`, `--ack-key-file`): a
  named person confirms that no systemd unit, cron job or colleague will start those
  containers during the window. The tool cannot detect those itself, so it refuses to
  proceed on an unverifiable claim.
- **Nothing is undone or retried.** A stage that refuses leaves the host exactly as it was
  at that moment — stopped and pinned — with its records on disk. Putting the restart
  policies back after a failure is a separate, deliberate call (`wal_fence.release()`),
  made once you know what happened.

It runs on the host from a checkout, standard library only:

```bash
cd backend && python3 -m app.wal_recovery_cli --help
```

The module docstrings of `backend/app/wal_recovery_cli.py`, `wal_fence.py` and
`wal_recovery.py` are the full reference.

## Security

- JWT authentication (access + refresh tokens with rotation); refresh tokens are
  blacklisted on logout and rotation
- Login rate limiting: 5 failures → 5-minute lockout per IP; API rate limiting: 100
  requests/minute per IP; agent connections: the same bad-key lockout plus 30 connection
  attempts/minute per IP
- Roles: `admin` and `user`. Docker, Users, Terminal and the Network Audit tab are admin-only
- Terminal requires an admin session **and** the host user's password
- TOTP two-factor authentication exists in the API (`POST /api/auth/totp/setup`, then
  `/api/auth/totp/confirm`), but the login screen cannot yet ask for the code — do not
  enable it on an account you use through the browser
- SMTP passwords encrypted at rest (Fernet, key derived from `GLASSOPS_SECRET_KEY`)
- Environment variables masked in container details
- IP allowlist with self-lockout prevention
- Runtime settings validated (username format, CIDR format, strict booleans)

### Container privileges

The container runs with a **least-privilege capability set instead of `privileged: true`**
— only the capabilities the host terminal (`nsenter` + `su`) and process kill actually need
(`SYS_ADMIN`, `SYS_PTRACE`, `SYS_CHROOT`, `DAC_READ_SEARCH`, `DAC_OVERRIDE`, `SETUID`,
`SETGID`, `KILL`, `CHOWN`); everything else is dropped. This removes host device access and
kernel-module loading. The host terminal and Docker control are still inherently high-trust
operations (`pid: host` and the Docker socket give broad host reach), so treat dashboard
admin access as equivalent to host root and keep it behind the network controls above.
`apparmor`/`seccomp` are set to `unconfined` because `nsenter` needs to enter the host
namespaces.

> `cgroup: host` in the compose file is **required** for per-container CPU/memory metrics
> and must not be removed — the `:ro` cgroup mount alone is not enough, as the kernel
> renders `/proc/<pid>/cgroup` paths relative to the reader's namespace.

### Docker socket proxy (optional)

By default the dashboard reaches the host Docker daemon over the mounted
`/var/run/docker.sock`, which is root-equivalent. An opt-in override routes it through a
socket proxy that permits only container **list / inspect / logs / start / stop / restart**
and blocks `create`, `exec`, `build`, `pull` and `remove`:

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml up -d
```

Every Docker feature in the UI keeps working; only the dangerous write verbs are denied.
This hardens the Docker API path only — the web terminal still enters the host namespaces,
so dashboard admin remains host-root-equivalent. Recommended for public or multi-tenant
deployments; optional for a trusted single-operator LAN.

## Contributing

Local and pull-request quality gates for Python, frontend and Compose changes, plus how to
run your changes: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 19, TypeScript, Vite, zustand, recharts, xterm.js, react-rnd |
| Backend | FastAPI, SQLite (aiosqlite), PyJWT, bcrypt, pyotp, cryptography (Fernet), aiosmtplib |
| Agent | psutil, pynvml, Docker SDK for Python, websockets |
| Infra | One container: nginx + supervisord on Python 3.12; the frontend is built in a Node 22 stage |

## License

MIT
