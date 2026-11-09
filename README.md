# GlassOps

GlassOps is a web dashboard for watching Linux servers from a browser: CPU, memory, disk,
GPU, network, processes, logs, Docker containers and host PM2 apps, laid out like a macOS desktop with
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

For NVIDIA GPU monitoring on the dashboard host, use `make up-gpu` instead of
`make up` after completing the [GPU setup](#gpu-monitoring).

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
| System Monitor | Overview gauges and history (Live / 5m / 1h / 6h / 24h / 7d), per-core CPU view, network rates and browser alert feed |
| GPU Monitor | NVIDIA GPU overview, detail/history charts and processes: utilization, VRAM, temperature, power, clocks and fan speed, with container attribution when available |
| Docker | Container start / stop / restart, detail drawer, separate Logs and Metrics windows, CPU/memory and available GPU/VRAM charts, plus Images, Volumes and Networks |
| Topology | Resource map of the selected host: hardware, Compose projects, standalone containers, host PM2 apps and other GPU processes; usage, limits and reservations |
| Network | Upload/download rates, interface details and active connections. The admin **Audit** tab shows events and traffic rollups collected by agents with `GLASSOPS_ENABLE_NET_AUDIT=true` |
| Process Viewer | Sortable process table with CPU/memory bars, search, kill with confirmation |
| Logs | Host system logs and container logs, with search and auto-refresh |
| Terminal | Web terminal (xterm.js) into the selected host; idle sessions are closed after 30 minutes |
| Settings | Profile, browser alert thresholds, Dock, wallpaper, server settings and email alerts; see [setting application](#applying-settings) |
| Users | Accounts and roles (`admin` / `user`), per-user host account mapping for the terminal, 2FA status |

Docker and Users are shown only to admins. Logs and Terminal icons are visible to users,
but reading logs and opening a terminal require an admin account. Process viewing is
available to users; killing a process requires an admin. Server and Email settings and the
Network Audit tab are also admin-only. Topology is available to both roles.

The desktop menu bar selects the monitored host. Server/Email settings apply to the
dashboard instance; personal display settings belong to the browser. See
[host switching](#3-switch-hosts-in-the-ui) for window behavior and mobile limits.

### Topology and PM2

Open **Topology** to see containers grouped by Compose project, standalone containers,
and PM2 apps running on the host. Hover or focus a node for details. Resource lines
distinguish actual GPU VRAM use from an assigned but idle GPU, CPU limits/pinned cores,
and memory limits/reservations. A GPU reservation describes device access, not exclusive
ownership or a reserved amount of VRAM.

The agent automatically discovers running host PM2 apps and reports instance counts,
CPU and memory use; no PM2 command or extra configuration is needed. This is monitoring:
PM2 start/stop controls are not provided. Older agents can omit PM2 or resource-limit
fields; Topology displays an agent-update notice in that case.

Unlock the layout to move nodes or groups, or use auto-arrange. Saved positions are kept
per host in this browser. Positions of other GPU processes are temporary and reset on a
reload or host switch.

### Docker windows and the Dock

On the desktop, click a container row for ports, networks, mounts and masked environment
variables. Its Logs and Metrics buttons open separate windows. Metrics covers CPU/memory
and shows GPU/VRAM charts when GPU use or a reservation is reported. With GPU collection
available, idle reservations can appear with zero use. Resource limits are also visible
in Topology.

Right-click a Dock icon (or focus it and press `Shift+F10`) to select a window, show all,
hide/minimize the group or quit its windows. Quit closes GlassOps windows; it does not
stop a host process or container. Docker's child windows are grouped under Docker.
The System Monitor badge counts active, unmuted browser alerts for the selected host.
**Settings > Dock** applies theme, size, magnification, position and auto-hide changes
immediately and remembers them in this browser.

## How It Works

```
┌─────────────────────────────────────────┐         ┌─────────────────────────────┐
│  Dashboard host (:7440)                 │         │  Remote host                │
│                                         │         │                             │
│  nginx ─── Frontend (React static)      │         │                             │
│    │                                    │         │                             │
│    ├─/api/  ─ Backend (FastAPI)         │         │                             │
│    └─/ws/   ─ WebSocket relay  ◄────────┼─ wss ─► │  Agent (psutil + Docker SDK)│
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

- **Metrics push** (agent → dashboard): system / GPU / Docker / PM2 / network / process snapshots
  every `GLASSOPS_COLLECT_INTERVAL` seconds.
- **RPC** (dashboard → agent → dashboard): when you restart a container, stream its logs,
  kill a process or open a terminal, the request travels to the selected agent and the answer
  comes back over the same connection. The built-in agent also handles these requests by
  RPC, using a loopback WebSocket inside the dashboard container. Remote connections use
  TLS through the [reverse proxy](#reverse-proxy-nginx).

Metrics are stored in a SQLite database in the data volume and are collected whether or not
anyone has the dashboard open.

## Requirements

- A Linux host with Docker Engine and Docker Compose v2.15.0 or newer
  ([`cgroup: host`](https://docs.docker.com/reference/compose-file/services/#cgroup)).
  The optional socket proxy needs Compose v2.24.4 or newer
  ([`!override`](https://docs.docker.com/reference/compose-file/merge/#replace-value)). The host-level features (process
  list, host logs, web terminal) rely on Linux namespaces (`pid: host`, `nsenter`).
- For NVIDIA GPU monitoring: a working host NVIDIA driver, NVIDIA Container Toolkit
  configured for Docker, and GPU access enabled for the container (see below).
- macOS with Docker Desktop is fine for trying the UI, but Docker runs in a Linux VM there:
  Process Viewer and Terminal show the VM, not macOS, and the `make agent-*` targets expect
  Linux tools.

## GPU Monitoring

The default `make up` works without a GPU or NVIDIA software. On an NVIDIA GPU host,
check that `nvidia-smi -L` lists the host's GPUs and install/configure the
[NVIDIA Container Toolkit for Docker](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html#configuring-docker).

With `.env` created as in Quick Start, start the dashboard with:

```bash
make up-gpu
```

This loads the base Compose file, the first existing `docker-compose.override.yml`
(or `.yaml`), then `docker-compose.gpu.yml`, in that order. It grants the
dashboard container access to all NVIDIA GPUs and sets `GLASSOPS_ENABLE_GPU=true`,
even if `.env` still contains the default `false`. This is the initial collection
default; a choice saved in Settings takes precedence. The built-in agent collects
this host's GPU metrics; a separate agent container is not needed on the dashboard host.
Host overrides participate in normal Compose merging. Set `GLASSOPS_BIND` and
`GLASSOPS_PORT` in `.env` to change the published address: adding another port mapping in
an override can keep both mappings. For an intentional replacement, use Compose's
`!override` syntax (v2.24.4+). The GPU overlay sets the initial GPU flag to true even if a
host override set it false.

The command builds the image and starts or recreates only the `glassops` service,
preserving its data volume. A recreation briefly interrupts the dashboard and metric
collection. The environment flag `GLASSOPS_ENABLE_GPU` sets the initial collection default but does
**not** grant GPU device access; adding device access requires recreating the container,
not just restarting it.

Use `make up-gpu` again when recreating or upgrading a GPU deployment (`git pull`,
then `make up-gpu`). The ordinary `up`, `prod`, and `update` targets do not load the
GPU overlay.
If you also use the socket proxy or other explicitly selected Compose files, retain the
complete file list as described under [Operations](#keeping-deployment-options).

Verify GPU access inside the running dashboard container:

```bash
docker compose exec glassops python -c 'import pynvml; pynvml.nvmlInit(); print("Detected GPUs:", pynvml.nvmlDeviceGetCount())'
```

The count should match the host's GPUs. In the dashboard, select the dashboard host
(`local` by default) and open **GPU Monitor** to check utilization and VRAM. If NVML
fails inside the container while `nvidia-smi` works on the host, check the Toolkit
configuration and recreate with `make up-gpu`.

To pause or resume collection, open **Settings > Server**, change **GPU Monitoring**,
and click **Save**. The built-in agent reads the saved choice on the next collection
cycle; no agent restart is needed. The choice survives agent and container restarts,
including a saved OFF choice when starting again with `make up-gpu`. Existing history
is retained. This switch applies only to the dashboard host, regardless of which host
is selected in the menu bar.

If the dashboard container cannot access an NVIDIA GPU, Settings blocks enabling
collection and explains that the host driver/Toolkit must be checked and the container
recreated with `make up-gpu`. An existing ON setting can still be switched OFF.

For an additional, remote GPU host, set `GLASSOPS_ENABLE_GPU=true` in `agent.env` and
use `make agent-up-gpu` there. The driver and Toolkit requirements also apply to that
host.

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
| `GLASSOPS_BIND` | `127.0.0.1` | Published web address. Keep loopback for local use or a TLS proxy on this host; remote agents can connect through the proxy. A LAN bind requires appropriate firewall/proxy controls |
| `GLASSOPS_SECRET_KEY` | *(generated)* | Master secret: signs sessions, derives the agent key, encrypts SMTP passwords. **Set it explicitly in production** (`openssl rand -hex 32`). If empty, one is generated and stored at `<data>/secret.key`. Weak or placeholder values are rejected at startup |
| `GLASSOPS_ADMIN_EMAIL` | `admin@glassops.local` | Email of the first admin account |
| `GLASSOPS_ADMIN_PASSWORD` | *(random)* | Password of the first admin. If unset, a one-time password is written to `<data>/initial_admin_password` (mode 0600) and must be changed at first login |
| `GLASSOPS_DB_PATH` | `/app/data/glassops.db` | SQLite database path inside the container |

**Collection**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_COLLECT_INTERVAL` | `1` | Integer seconds between collection cycles, minimum 1. Collection work also takes time |
| `GLASSOPS_ENABLE_DOCKER` | `true` | Populate Docker's Containers tab with snapshots. Off leaves the app visible but the list empty; it does not revoke Docker API access |
| `GLASSOPS_ENABLE_GPU` | `false` | Initial GPU collection default; saved Settings choice takes precedence for the dashboard. `make up-gpu` sets this to true and grants device access |
| `GLASSOPS_LOCAL_AGENT_ID` | `local` | Built-in agent ID. Keep the default for browser terminal fallback; if customized, add an explicit host account mapping in Users |
| `GLASSOPS_AGENT_ID` | *(set in `agent.env`)* | Unique remote host ID: 1–64 letters, digits, `_` or `-`; do not reuse the built-in ID. In the dashboard container it is overridden with `GLASSOPS_LOCAL_AGENT_ID` |
| `GLASSOPS_AGENT_KEY` | *(derived)* | Remote agents only: the key a remote agent presents. Get it from the dashboard host with `docker compose exec glassops python -m app.secret_bootstrap agent`. The built-in agent derives it itself |
| `GLASSOPS_RPC_TIMEOUT` | `30` | Timeout in seconds for a single RPC response; not the lifetime of live log or terminal streams |

**Terminal**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_TERMINAL_USER` | *(unset)* | Host user the local terminal logs in as when the dashboard user has no mapping in the Users app |
| `GLASSOPS_ALLOW_LOGIN_PROMPT` | `false` | With no terminal user configured, show the host `login` prompt instead of refusing. Lets an admin log in as **any** host account, root included — off by default |

**Network access, proxies, browsers**

| Variable | Default | What it does |
|----------|---------|--------------|
| `GLASSOPS_ALLOWED_IPS` | *(all)* | Comma-separated CIDR allowlist of client addresses. Checked against the real client IP — behind a proxy, also set `GLASSOPS_TRUSTED_PROXIES` |
| `GLASSOPS_TRUSTED_PROXIES` | `127.0.0.1,::1` (backend) | Backend trusts bundled nginx on loopback. Add the direct proxy peer's non-loopback CIDR as seen by the container for edge nginx to restore `X-Forwarded-For` |
| `GLASSOPS_FORCE_SECURE_COOKIES` | `false` | Mark auth cookies `Secure` when TLS is terminated by an upstream proxy (see Production Deployment) |
| `GLASSOPS_ALLOWED_ORIGINS` | *(same host)* | Hostnames/URLs allowed by browser WebSocket and HTTP CSRF checks. Only hostnames are compared (scheme/port ignored). A configured list replaces matching the request `Host` hostname |
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

### Applying settings

| Setting | Where to change it | When it takes effect |
|---|---|---|
| Dashboard GPU collection | Settings > Server > GPU Monitoring, then Save | Next collection cycle, provided the container already has GPU access |
| Docker collection, collection interval, local terminal fallback, IP allowlist | `.env` on the dashboard host | Standard setup: `make up` or `make up-gpu`; with extra overlays, use `docker compose up -d --build` with the [complete file selection](#keeping-deployment-options) |
| Remote agent collection | `agent.env` on that host | Standard setup: `make agent-up` or `make agent-up-gpu`; with a private CA/custom overlay, use `docker compose up -d --build` with the full `-f` list |
| Email alerts | Settings > Email | Saved server-side; applies across monitored hosts |
| Browser alert thresholds, Dock and wallpaper | Corresponding Settings tab | Applied in this browser; not synchronized to other browsers |

The non-GPU fields in **Settings > Server** currently save values to the database, but
**Restart Agent / Apply IP Rules do not connect those saved values to the running
configuration**. Use the environment-file paths above for those settings. A plain
container restart keeps its old environment. GPU collection reads its saved choice each
cycle; neither saving it nor restarting can add missing device access.

## Make Commands

Dashboard host:

```bash
make up          # Stop the Compose project, then build/start; applies .env changes
make up-gpu      # Build/start with NVIDIA GPU access (saved monitoring choice retained)
make down        # Stop
make restart     # Restart the container (does not re-read .env)
make logs        # Follow logs
make status      # Container status and /health at localhost:7440 by default
make shell       # Shell inside the container
make build       # Build the image only
make prod        # Rebuild from scratch (no cache) and start
make update      # Basic deployment: git pull, rebuild without cache, start
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
`agent-up-gpu` grants device access; also set `GLASSOPS_ENABLE_GPU=true` in `agent.env`.
These targets specify their Compose files explicitly, so additional agent overlays must
also be named explicitly (see the private CA example below).

`make status` and the printed URLs use Make's `PORT` (default `7440`), independently of
`.env`'s `GLASSOPS_PORT` and `GLASSOPS_BIND`. With a custom port use
`make status PORT=7550` if loopback is reachable, or query `/ready` at your configured URL.
For GPU updates use `git pull` followed by `make up-gpu`; for additional Compose overlays
use [the complete file selection](#keeping-deployment-options).

The quality gates (`make quality` and friends) are described in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Metrics History

History queries select a resolution by the **length of the requested interval**:

| Requested interval | Resolution | Stored in |
|-----|------------|-----------|
| Up to 1 hour | Raw, at the actual collection cadence (default about 1 second) | `metrics` |
| Over 1 hour, up to 24 hours | 1-minute average | `metrics_downsampled` |
| Over 24 hours (UI offers up to 7 days) | 5-minute average | `metrics_downsampled` |

Both summary resolutions normally become eligible for cleanup after **7 days**;
summaries still needed by unfinished aggregation/cleanup are retained longer. Raw rows older than an hour are
eligible for cleanup only once both summaries cover them; incomplete aggregation can
retain raw rows longer. These retention rules are separate from the query resolution:
a short query far in the past may have no raw rows left.

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

The Linux deployment exposes host resources to the agent:

- `pid: host` — sees all host processes
- `/var/log` mounted — reads host system logs
- `/proc` mounted — supports host process and resource inspection
- Docker socket — manages host Docker containers
- `nsenter` — the terminal enters the host's namespaces (Linux only)

Disk **capacity/usage** is measured at the agent's `/` filesystem; it is not an inventory
of every host disk or mount. General Network counters and interface details use the
agent's network namespace: the default dashboard uses Docker bridge networking, while
the standalone agent uses host networking. Network Audit separately collects host
connection metadata. Interpret these fields according to the deployment's mounts and
namespaces.

## Multi-Host Monitoring

By default GlassOps monitors the server it is installed on. To add more hosts, run the
agent-only container on each one; the dashboard shows them all through the host dropdown in
the menu bar.

### 1. Make the TLS endpoint reachable

Set up the [TLS reverse proxy](#reverse-proxy-nginx) on the dashboard host and allow the
agents to reach its HTTPS port. The agent connects outbound to
`wss://ops.example.com/ws/agent`; it needs no inbound published port of its own.
Keep `GLASSOPS_BIND=127.0.0.1` when the proxy runs on the dashboard host. Port `7440`
serves plain HTTP internally and does not need to be exposed directly to remote agents.

### 2. Install the agent on each remote host

```bash
git clone https://github.com/ParkHyeonSeong/glassops.git
cd glassops
cp agent.env.example agent.env
```

Edit `agent.env`:

```env
GLASSOPS_AGENT_ID=dev10                              # unique; reserve local for the dashboard
# Backend's derived agent key — get it from the dashboard host with:
#   docker compose exec glassops python -m app.secret_bootstrap agent
GLASSOPS_AGENT_KEY=<backend derived agent key>
# Use wss:// (TLS). Plaintext ws:// exposes the agent key + all RPC (shell/exec)
# to the network; terminate TLS at a reverse proxy or the dashboard host.
GLASSOPS_SERVER_URL=wss://ops.example.com/ws/agent
GLASSOPS_REQUIRE_AGENT_TLS=true                      # reject plaintext remote connections
GLASSOPS_ENABLE_DOCKER=true
GLASSOPS_ENABLE_GPU=true                             # set false if no NVIDIA GPU
```

> **Remote agents must use `wss://`.** The connection carries the agent key and RPC
> commands (including a shell on the agent host); over plaintext `ws://` anyone on the
> network path can capture the key and inject commands. Put a TLS reverse proxy (Caddy,
> nginx or Traefik with Let's Encrypt) in front of the dashboard, or use a self-signed
> certificate and mount its CA bundle as described below. The default
> `GLASSOPS_REQUIRE_AGENT_TLS=false` only warns about plaintext remote connections;
> set it to `true` to enforce this requirement.

Then start the agent:

```bash
make agent-up-gpu      # NVIDIA GPU host
# or
make agent-up          # CPU/Docker only
make agent-logs        # follow its logs
```

For a private CA, create a host-local `compose.agent-ca.yml` (keep local deployment files
out of Git), using your actual CA bundle's absolute host path:

```yaml
services:
  glassops-agent:
    volumes:
      - /etc/glassops/ca.pem:/etc/glassops/ca.pem:ro
```

Set `GLASSOPS_AGENT_CA=/etc/glassops/ca.pem` in `agent.env`: this is the **container** path.
Start with the extra file explicitly; the Make targets do not discover this overlay:

```bash
GLASSOPS_DOCKER_GID=$(getent group docker | cut -d: -f3) \
  docker compose -f docker-compose.agent.yml -f compose.agent-ca.yml up -d --build
```

For a GPU agent, include `-f docker-compose.agent.gpu.yml` before `-f compose.agent-ca.yml`
and keep `GLASSOPS_ENABLE_GPU=true`. Use the same file list for subsequent updates and
recreations. Certificate and hostname verification remain enabled.

### 3. Switch hosts in the UI

The desktop menu bar shows a host dropdown after receiving data for more than one agent.
Selecting a host changes System Monitor, GPU Monitor, Topology, Network, the Docker list
and Process Viewer; Terminal reconnects to that host. Already-open container Metrics
windows and live log streams retain the host/container they were opened for.

Choose the host before opening Logs or querying container log history; reopen Logs after
switching hosts, and keep the original host selected for an existing container's history
query. Finish or cancel a process-kill confirmation before changing hosts. Server/Email
settings remain dashboard-wide. The mobile app grid currently has no host selector, and
the separate container windows described above are a desktop flow.

What works across hosts:

- Real-time metrics and Topology (CPU, memory, disk, GPU, network, processes, containers, PM2)
- Container start / stop / restart, container detail, images, volumes, networks
- Container log streaming (live tail with autoscroll-follow, plus historical date-range queries)
- System log viewer (host log files are mounted into the agent)
- Process kill (subject to the agent's process privileges)
- Terminal — the host user comes from the dashboard user's mapping for that host in the
  Users app (the `GLASSOPS_TERMINAL_USER` fallback applies to the local agent only)

## Production Deployment

### Reverse proxy (nginx)

GlassOps binds to `127.0.0.1` by default. The example below runs nginx on the dashboard
host and forwards both browser and agent traffic to that loopback port. Configure TLS
before exposing it to remote clients. If your proxy reaches GlassOps through a LAN
address, adjust `GLASSOPS_BIND` and the upstream URL and restrict access to the proxy.

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
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

> Without the `/ws/` block: System Monitor stays at "Connecting...", Terminal shows
> "Disconnected".

### HTTPS

Enable HTTPS on the proxy, for example with an installed Certbot nginx integration:

```bash
sudo certbot --nginx -d ops.example.com
```

> **Terminating TLS at an upstream proxy?** The bundled edge speaks plain HTTP internally
> and deliberately does **not** trust a forwarded `X-Forwarded-Proto` from it (that header
> is client-spoofable at a loopback edge). So when an external proxy terminates TLS, set
> `GLASSOPS_FORCE_SECURE_COOKIES=true` so auth cookies still get the `Secure` flag.

Recreate the container with the same deployment options after changing `.env`.
Auth cookies are always `HttpOnly`. In secure cookie mode the frontend does not persist
token values in `sessionStorage`, but tokens are still returned in login JSON and held
in frontend memory; this is not a guarantee that page scripts cannot access them.

### IP restriction

**Option A — in your reverse proxy** (recommended):
```nginx
allow 10.0.0.0/8;
allow 192.168.0.0/16;
deny all;
```

**Option B — in GlassOps**, set `.env` and recreate the container:
```env
GLASSOPS_ALLOWED_IPS=10.0.0.0/8,192.168.0.0/16
```

Use `make up` for a basic deployment or `make up-gpu` for the standard GPU setup. With
additional files such as the socket proxy, select the [complete file list](#keeping-deployment-options)
and use `docker compose up -d --build` instead. Saving the IP list in Settings and clicking Apply IP Rules currently does
not regenerate active rules; use `.env` or the proxy configuration above.

> **Behind a reverse proxy?** GlassOps matches the real client IP. If you front it with an
> LB/TLS proxy, set `GLASSOPS_TRUSTED_PROXIES` to the direct proxy peer's CIDR **as seen by
> the bundled nginx**. For a proxy on the Docker host this may be the bridge gateway,
> even though the upstream URL uses `127.0.0.1`. Edge nginx skips loopback entries; verify
> the actual peer instead of copying the upstream URL's address. Otherwise the allowlist
> and rate limits see the proxy rather than the client. If that peer is not established,
> apply IP restrictions at the external proxy first.

### Terminal user

The web terminal is admin-only and runs as a host user, resolved in this order:

1. **Per-user host mappings** in the **Users** app give each dashboard user a specific host
   account per host. This is the only option for remote hosts.
2. `GLASSOPS_TERMINAL_USER=ubuntu` — one host user for the local terminal, used when the
   dashboard user has no mapping.
3. `GLASSOPS_ALLOW_LOGIN_PROMPT=true` opts into a host `login` prompt, which lets an admin
   authenticate as **any** host account, including root. **Off by default** — with none of
   the above configured, the terminal is refused rather than exposing a login prompt.

The agent enters the host and runs `su - <user>` for a mapped/default account, or `login`
for the opt-in prompt. Whether another password is requested follows the host's
authentication policy; GlassOps does not enforce a second password for every terminal.

### Secret key

Generate a key before the first production start:

```bash
openssl rand -hex 32
```

Store the output as the value of `GLASSOPS_SECRET_KEY` in the deployment's private `.env`
file. A shell assignment alone does not update `.env`. Preserve the existing key when
updating or restoring an installation.

It signs sessions, derives the agent key that remote agents present, and encrypts stored
SMTP passwords. If you leave it empty, GlassOps generates one and keeps it at
`<data>/secret.key` — fine for a single host, but back that file up with the data volume.
Changing or losing the key logs everyone out, disconnects remote agents until their key is
updated, and makes stored SMTP passwords undecryptable.

## Operations

### Keeping deployment options

Run maintenance from the existing deployment checkout, using its `.env` and Compose
project name so commands address the same data volume. The examples below use
`docker compose`; keep the same file selection for **every** stop, run, build and up.
A basic deployment uses automatic base/host-override discovery. For a GPU deployment
without a host override, select the files in the current shell first:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
```

If a host override exists, insert its actual name between those files, for example
`docker-compose.yml:docker-compose.override.yml:docker-compose.gpu.yml` (use `.yaml` if
that is your file). Explicit file selection disables automatic override discovery.
For the socket proxy, also include `docker-compose.socket-proxy.yml` before the GPU file;
it replaces the volume list, so include any required custom mounts in the final merged
configuration. Use `docker compose config --quiet` to validate the selection; inspect
the resolved mounts/ports locally if customized, keeping environment secrets private.
Retain this shell/file selection through maintenance. A new shell must select it again.

### Health and readiness

Two unauthenticated endpoints, both served on the web port:

| Endpoint | Answers | Meaning |
|---|---|---|
| `GET /health` | `200 {"status":"ok"}` | The process is up. Says nothing about storage |
| `GET /ready` | `200` or `503` | `200` while the database is accepting writes; `503` once it is not (fail-stop, shutting down, or an operation whose outcome could not be confirmed). Runs no SQL, so it still answers when the database is stuck |

In plain terms: `/health` says "alive", `/ready` reports whether the process is accepting
database writes. It reads in-process state, not a new SQL probe or proof that a particular
sample was saved. The deployment Compose healthcheck polls `/ready`. A `503` body names the condition (`database`) and whether
`restart_required` is true — in which case the process will not recover on its own: read
`make logs` for the reason (it is logged, not served) and restart the container.

### Backup and restore

Server data — accounts, password hashes, host mappings, runtime settings, email alert
configuration, audit records and metric history — lives in `/app/data` by default, in
the Compose project's `glassops_data` volume. With an empty `GLASSOPS_SECRET_KEY`, the
generated master secret (`secret.key`) is there too. `.env` and host-local Compose files
live outside the volume and must be kept with the backup.

Dock, wallpaper, browser alert thresholds and Topology layouts live in browser storage;
they are not included in this server backup. Email thresholds are server-side and are
included. The commands below assume the default `/app/data/glassops.db` and volume layout;
if `GLASSOPS_DB_PATH` or mounts differ, back up and restore their actual locations.

Back up with the container **stopped**. The database is written to continuously while it
runs, and a copy taken mid-write can be inconsistent:

```bash
(
set -e
umask 077
GLASSOPS_BACKUP_DIR=$(mktemp -d "$HOME/glassops-backup.XXXXXX")
printf 'Backup directory: %s\n' "$GLASSOPS_BACKUP_DIR"
cp .env "$GLASSOPS_BACKUP_DIR/.env"
for glassops_override in docker-compose.override.yml docker-compose.override.yaml; do
  if [ -f "$glassops_override" ]; then cp "$glassops_override" "$GLASSOPS_BACKUP_DIR/"; fi
done
docker compose down &&
docker compose run --rm --no-deps --entrypoint "" -v "$GLASSOPS_BACKUP_DIR:/backup" glassops \
  tar czf /backup/glassops-data.tgz -C /app/data . &&
  docker compose up -d
)
```

Check that the archive command succeeded before continuing. Keep this private directory
outside the checkout/build context, together with any other local Compose files and the
file selection used. Treat it like a password file: it holds secrets and every account.

To restore — on the same machine, or on a new one after `git clone`, copying the saved
`.env` and host overrides into place, selecting the same Compose files/project, and running
`docker compose build`. Set `GLASSOPS_BACKUP_DIR` in your shell to the absolute backup
directory printed above before running the restore commands:

```bash
docker compose down &&
docker compose run --rm --no-deps --entrypoint "" -v "$GLASSOPS_BACKUP_DIR:/backup:ro" glassops \
  sh -c 'set -e; tar tzf /backup/glassops-data.tgz >/dev/null; rm -rf /app/data/*; tar xzf /backup/glassops-data.tgz -C /app/data' &&
  docker compose up -d
```

This replaces whatever is in the volume with the backup. Use the same `.env` the backup was
taken with: a different `GLASSOPS_SECRET_KEY` would log everyone out, break stored SMTP
passwords and disconnect remote agents. This is also how you move GlassOps to another
server — the transfer tool two sections below is only for the old-schema upgrade.

### Updating

For a basic deployment:

```bash
make update      # git pull + rebuild without cache + start
```

For a GPU deployment using the standard host override:

```bash
git pull
make up-gpu
```

For additional Compose files such as the socket proxy, keep the
[full file selection](#keeping-deployment-options) and run:

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

The data volume is kept. If upgrading a legacy database, check the next section's schema
conditions first; creation date alone does not establish whether transfer is needed.

### Upgrading a legacy database

The database layout changed in September 2026 (metric aggregation tables, one database
boundary). Monitoring history from before that point is **not** carried over — only your
accounts, their host mappings, alert/email settings, revoked tokens, the audit log and the
master secret are. A small tool, `app.account_state_transfer`, moves exactly that into a
fresh database. It is one-way and one-time: it refuses a source database made by the
current build. This is specifically for the source schema represented by build `817e373`
with an **empty `runtime_config` table**, not an arbitrary old database. The tool checks
these conditions and refuses incompatible sources. Network Audit events/rollups are also
excluded, separately from the account/action `audit_log` that is preserved.

The steps, in order. The commands run the tool inside a throwaway container of the new
image because the data volume is normally reachable only as root; from a checkout on the
host the same tool is `cd backend && python3 -m app.account_state_transfer …` (standard
library only). Proceed to each next step only after the current step succeeds; a refusal
requires inspection, not continuing down the command list.

1. Take a [backup](#backup-and-restore), then stop the old version. Keep the volume and
   old `.env`/master secret. Fetch and build the new version, but do not start it:

   ```bash
   docker compose down &&
   git pull &&
   docker compose build
   ```

2. Prepare a private transfer directory outside the repository:

   ```bash
   umask 077
   GLASSOPS_TRANSFER_DIR=$(mktemp -d "$HOME/glassops-transfer.XXXXXX")
   ```

   The bundle contains the master secret in plain text — treat the directory like a
   password.

   If the old installation used an explicit `GLASSOPS_SECRET_KEY`, its value may differ
   from a leftover `secret.key` file. Keep the exact old environment value and prepare it
   through Compose's environment resolution (no key is printed):

   ```bash
   docker compose run --rm --no-deps --entrypoint "" -v "$GLASSOPS_TRANSFER_DIR:/transfer" glassops python3 -c '
   import os
   from pathlib import Path
   from app.secret_bootstrap import resolve_secret
   raw = os.environ.get("GLASSOPS_SECRET_KEY", "").strip()
   if not raw:
       raise SystemExit("Expected the old explicit GLASSOPS_SECRET_KEY; use the existing secret.key otherwise")
   keydir = Path("/transfer/keydir")
   keydir.mkdir(mode=0o700)
   keyfile = keydir / "secret.key"
   with keyfile.open("x") as out:
       os.chmod(keyfile, 0o600)
       out.write(resolve_secret(raw, "/app/data"))'
   ```

3. Export **with the backend stopped**. For a generated key, use the existing
   `/app/data/secret.key`. For the explicit-key preparation above, add
   `--data-dir /transfer/keydir` to this command:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" -v "$GLASSOPS_TRANSFER_DIR:/transfer" glassops \
     python3 -m app.account_state_transfer export \
       --source-db /app/data/glassops.db --bundle /transfer/bundle
   ```

   Continue only on success. Keep the same key configuration for the new installation.

4. Move the old database aside, inside the volume:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" glassops sh -c \
     'set -e; mkdir /app/data/old; for f in glassops.db glassops.db-wal glassops.db-shm; do if [ -e "/app/data/$f" ]; then mv "/app/data/$f" /app/data/old/; fi; done; ls -l /app/data/old'
   ```

5. Create the fresh database with the new build — **without starting the server**. If you
   simply started it, its built-in agent would begin storing metrics within seconds and the
   restore would refuse the target as not fresh:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" glassops python3 -c '
   import asyncio
   from app.database import init_db, close_db, CloseVerdict
   async def main():
       await init_db()
       verdict = await close_db()
       print(verdict)
       if verdict != CloseVerdict.CLOSED:
           raise SystemExit("Database did not close cleanly; inspect before restoring")
   asyncio.run(main())'
   ```

   It prints `CloseVerdict.CLOSED` and a note about a one-time admin password; ignore the
   note — the restore removes that placeholder account.

6. Restore:

   ```bash
   docker compose run --rm --no-deps --entrypoint "" -v "$GLASSOPS_TRANSFER_DIR:/transfer" glassops \
     python3 -m app.account_state_transfer restore \
       --bundle /transfer/bundle --target-db /app/data/glassops.db --run-dir /transfer/run-1
   ```

   Success is exit `0`, a JSON report on stdout and `$GLASSOPS_TRANSFER_DIR/run-1/COMPLETE`. Do
   not start the new version before that file exists. Exit `2` is a refusal with the reason
   on stderr. Retry only when the refusal explicitly gives `RETRY_GUIDANCE`, using the
   same bundle and a **new** `--run-dir`. `MANUAL_VERIFICATION_REQUIRED` means the fresh
   target state could not be confirmed. Exit `1` is an unexpected error. Inspect any
   failure before continuing; do not assume rollback or retry safety.

7. Start the new version and log in with your existing accounts:

   ```bash
   docker compose up -d
   ```

   Once the restored accounts and settings are verified, securely retain or remove the
   private transfer directory according to your backup policy. Keep the full backup and
   old database until rollback is no longer needed.

The module docstring of `backend/app/account_state_transfer.py` is the full contract.

### Guarded WAL recovery

SQLite keeps recent writes in `glassops.db-wal` and periodically folds them back into the
main file (a "checkpoint"). If checkpoints stall — typically a connection held open for a
very long time — the WAL keeps growing, into the hundreds of gigabytes if left alone, while
the database itself stays consistent. Folding it back safely needs a moment when nothing can
write: the containers stopped, and nothing allowed to restart them meanwhile.

The recovery tool has a narrower prerequisite than a large WAL: a sentinel row must be
present when reading the database **with its WAL**, and absent from the main DB file
alone. The WAL must still exist and be nonempty after fencing. If a normal shutdown
already checkpointed it, or the sentinel is already in the main file, recovery refuses.
Use this procedure only when those conditions apply; it is not a general WAL shrink command.

`app.wal_recovery_cli` runs that procedure in a fixed order and stops at the first refusal:

| Stage | What happens | Exit code on refusal |
|---|---|---|
| fence | Records the restart policy of every container in `--scope`, disables them, stops the containers gracefully (`docker stop --timeout=-1`, never a kill) and writes a manifest | 10 |
| claim | Takes the one-shot lease the manifest issues | 11 |
| recovery | Verifies the WAL-only sentinel, backs up the database, verifies the backup and checkpoints the WAL; `--probe` tables are compared before and after | 12 |
| terminal | Confirms the run is recorded as COMPLETED | 13 |
| release | Puts the restart policies back. The containers stay stopped — you start them | 14 |

Exit `0` is success; `2` means bad arguments, and nothing on the host was touched.
Before running it:

- `--scope` must exactly cover all containers sharing the DB mount or holding the Docker
  socket, including a socket proxy and matching containers in other Compose projects.
  The tool refuses an incomplete or broader set; plan the interruption for that actual scope.
- It requires a **signed acknowledgement** (`--host-id`, `--ack-file`, `--ack-key-file`): a
  named person confirms that no systemd unit, cron job or colleague will start those
  containers during the window. The tool verifies the acknowledgement's signature,
  host, scope and time; it cannot verify the absence of those external actions.
- **No automatic rollback or retry.** A refusal can happen before changes, after only
  some containers were pinned/stopped, or during partial restoration of restart policies.
  Check the failed stage, its records (including `PREPARED` when present) and actual
  container state. Do not assume everything is stopped or released. A separate
  `wal_fence.release()` call must follow that inspection and the claim/recovery state
  required by its contract.

Run it on the actual Linux Docker daemon host, with access to the DB mount, Docker,
`lsof` and the required host process/mount visibility. It uses Python's standard library
but still requires those host tools and permissions. View arguments without recovery:

```bash
cd backend && python3 -m app.wal_recovery_cli --help
```

The module docstrings of `backend/app/wal_recovery_cli.py`, `wal_fence.py` and
`wal_recovery.py` are the full reference.

## Security

- JWT authentication (access + refresh tokens with rotation); rotation revokes the old
  refresh token. Logout revokes the access token and a refresh token supplied in its body
- Login rate limiting: 5 failures → 5-minute lockout per IP; API rate limiting: 100
  requests/minute per IP; agent connections: the same bad-key lockout plus 30 connection
  attempts/minute per IP
- Roles: `admin` and `user`; Docker, Users, log reads, process kills, Terminal,
  Server/Email settings and Network Audit require admin privileges
- Terminal requires an active admin session and a host mapping or the local fallback;
  host password prompts follow the host's `su` / `login` policy
- Two-factor authentication (TOTP) is **not available yet**: enrolment is refused with
  `501` until the login screen can prompt for a code (see below)
- SMTP passwords encrypted at rest (Fernet, key derived from `GLASSOPS_SECRET_KEY`)
- Environment variables masked in container details
- IP allowlist configured at the proxy or through `.env` and container recreation;
  review your client/proxy addresses before applying it
- Saved Server values are format-validated; only the GPU switch is currently consumed
  live (see [Applying settings](#applying-settings))

### Two-factor authentication (not available yet)

The backend can verify TOTP codes, but the login screen cannot prompt for one, so an account
that turned 2FA on would be locked out of the browser. Until the login screen supports it,
the enrolment endpoints (`POST /api/auth/totp/setup` and `POST /api/auth/totp/confirm`)
answer `501 Not Implemented` and change nothing. Nothing in the UI enables 2FA, so most
installations are unaffected.

If an account was enabled through the API before this, it still needs its code on API logins
and cannot log in through the browser. To see whether any such account exists, and to switch
2FA off for one, take a backup and run these with the container stopped (`docker compose down`).
The examples use the default DB path and refuse to create a missing DB; adapt the path if
configured differently. Resume with `docker compose up -d`, preserving the
[same deployment files](#keeping-deployment-options):

```bash
docker compose run --rm --no-deps -T --entrypoint "" glassops python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("file:/app/data/glassops.db?mode=rw", uri=True)
print(db.execute("SELECT email FROM users WHERE totp_enabled = 1").fetchall())
EOF
```

```bash
docker compose run --rm --no-deps -T --entrypoint "" glassops python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("file:/app/data/glassops.db?mode=rw", uri=True)
db.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE email = ?", ("alice@example.com",))
db.commit()
EOF
```

### Container privileges

The container runs with a **least-privilege capability set instead of `privileged: true`**
— only the capabilities the host terminal (`nsenter` + `su`) and process kill actually need
(`SYS_ADMIN`, `SYS_PTRACE`, `SYS_CHROOT`, `DAC_READ_SEARCH`, `DAC_OVERRIDE`, `SETUID`,
`SETGID`, `KILL`, `CHOWN`); other capabilities are dropped. The base deployment does not
request NVIDIA devices; the GPU overlay explicitly grants them. The host terminal and Docker control are inherently high-trust
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
socket proxy that permits container **list / inspect / logs / start / stop / restart**,
image/volume/network reads and daemon info, while blocking `create`, `exec`, `build`,
`pull` and `remove`. This overlay requires Compose **v2.24.4+** for its `!override` tag:

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml up -d
```

Every Docker feature in the UI keeps working; only the dangerous write verbs are denied.
For GPU as well, start both services with the full file list:

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml -f docker-compose.gpu.yml up -d --build
```

`make up-gpu` starts only `glassops` and will not start the proxy. Retain the socket-proxy
file for subsequent updates and maintenance; include any host overrides explicitly and
check custom mounts because the proxy replaces the base volume list.

This hardens the built-in agent's Docker API path only; remote agents keep their own
Docker access configuration. The web terminal still enters host namespaces, so dashboard
admin remains host-root-equivalent. Use it within the same trusted-admin deployment
model; it does not establish isolation for untrusted tenants.

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
