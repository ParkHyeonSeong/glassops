# GlassOps

A macOS-style server monitoring dashboard. All-in-one alternative to Grafana + Portainer — single container, zero configuration.

## Quick Start

```bash
git clone https://github.com/your-username/glassops.git
cd glassops
make up
```

Open **http://localhost:7440** and log in.

On first run GlassOps creates the admin account (`admin@glassops.local`) with a
**random one-time password** written to `data/initial_admin_password` (mode 0600, in
the mounted data volume — `/app/data/initial_admin_password` inside the container).
Read it, log in, and you'll be required to change it immediately:

```bash
cat data/initial_admin_password
```

> Prefer your own password? Set `GLASSOPS_ADMIN_PASSWORD` in `.env` before the first run.
> (If that file can't be written, startup fails with an error instead of logging the password — fix the data-dir permissions or set `GLASSOPS_ADMIN_PASSWORD`.)

## What's Inside

| App | Description |
|-----|-------------|
| System Monitor | Real-time CPU, Memory, Disk gauges + time-series charts (Live / 5m / 1h / 6h / 24h / 7d) |
| GPU Monitor | Multi-GPU dashboard: utilization, VRAM, temperature, power, clocks, fan speed, per-process VRAM |
| Docker Manager | Containers (start/stop/restart), live log streaming with autoscroll-follow, date-range historical view, Images, Volumes, Networks tabs |
| Network Analyzer | Upload/Download rates, active connections table, interface info |
| Process Viewer | Sortable process table with CPU/MEM bars, search/filter, kill with confirmation |
| Log Viewer | System logs + Docker container logs, search, auto-refresh |
| Terminal | Web-based terminal (xterm.js), JWT-authenticated, idle timeout |
| Settings | Profile, agents, server config (runtime toggles), alert thresholds, SMTP email, wallpaper |

## Architecture

Single Docker container with the dashboard + a built-in local agent. Additional hosts run an agent-only container that connects back over WebSocket.

```
┌─────────────────────────────────────────┐         ┌─────────────────────────────┐
│  GlassOps Host (:7440)                  │         │  Remote Host (e.g. dev10)   │
│                                         │         │                             │
│  nginx ─── Frontend (React static)      │         │                             │
│    │                                    │         │                             │
│    ├─/api/  ─ Backend (FastAPI)         │         │                             │
│    └─/ws/   ─ WebSocket relay  ◄────────┼─ ws ──► │  Agent (psutil + Docker SDK)│
│                                         │ metrics │   • pushes metrics          │
│  Local Agent (built-in via supervisord) │ + RPC   │   • serves RPC requests     │
│                                         │         │     (logs / actions / etc.) │
└─────────────────────────────────────────┘         └─────────────────────────────┘
```

The agent WebSocket carries two flows on a single connection:

- **Metric push** (agent → backend): system / GPU / docker / network / process snapshots
- **Bidirectional RPC** (backend → agent → backend): the dashboard issues docker actions, container log streams, process kills, etc. against the selected agent. Local agent calls bypass RPC for zero round-trip latency.

The MenuBar dropdown picks which agent's data the entire dashboard reflects — every panel (System Monitor, Docker Manager, Logs, Process Viewer) follows the selection.

## Requirements

- Docker + Docker Compose v2
- That's it.

## Configuration

Copy and edit `.env`:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GLASSOPS_PORT` | `7440` | Web UI port |
| `GLASSOPS_BIND` | `127.0.0.1` | Bind address of the published port. Use the host's LAN IP (e.g. `10.0.0.9`) to allow remote agents to connect, or `0.0.0.0` (combine with firewall) |
| `GLASSOPS_SECRET_KEY` | *(auto-generated if empty)* | Master secret. Root of JWT signing + derived agent/SMTP subkeys. **Set a strong value in production** (`openssl rand -hex 32`); if left empty one is generated and persisted at `<data>/secret.key`. Weak/placeholder values are **rejected at startup** |
| `GLASSOPS_ADMIN_EMAIL` | `admin@glassops.local` | Initial admin email |
| `GLASSOPS_ADMIN_PASSWORD` | *(random)* | Initial admin password. If unset, a random one-time password is written to `<data>/initial_admin_password` (0600) and a change is forced on first login |
| `GLASSOPS_DB_PATH` | `/app/data/glassops.db` | SQLite database path |
| `GLASSOPS_AGENT_ID` | `local` | Agent identifier (this server's own agent) |
| `GLASSOPS_AGENT_KEY` | *(auto)* | Auto-derived from SECRET_KEY for the built-in agent. On **remote** agents set it to the backend's derived agent key: `docker compose exec glassops python -m app.secret_bootstrap agent` |
| `GLASSOPS_COLLECT_INTERVAL` | `1` | Metrics collection interval (seconds, 1-60) |
| `GLASSOPS_ENABLE_DOCKER` | `true` | Enable Docker container monitoring |
| `GLASSOPS_ENABLE_GPU` | `false` | Enable NVIDIA GPU monitoring (requires pynvml) |
| `GLASSOPS_LOCAL_AGENT_ID` | `local` | Agent ID treated as "local" by the backend. Local-agent REST calls bypass RPC and hit the docker socket directly |
| `GLASSOPS_RPC_TIMEOUT` | `30` | Timeout (s) for backend → agent RPC calls (logs, actions, etc.) |
| `GLASSOPS_TERMINAL_USER` | *(login prompt)* | Host user for web terminal |
| `GLASSOPS_ALLOWED_IPS` | *(all)* | Comma-separated CIDR whitelist. Matches the **real** client IP — behind an upstream proxy, set `GLASSOPS_TRUSTED_PROXIES` so the whitelist sees through it |
| `GLASSOPS_TRUSTED_PROXIES` | `127.0.0.1,::1` | CIDRs of upstream proxies whose forwarded headers are believed. Drives backend IP/scheme trust **and** nginx `real_ip`, keeping the whitelist + per-IP rate limits correct behind an LB/TLS proxy |
| `GLASSOPS_ENABLE_NET_AUDIT` | `false` | Record network connection events + traffic rollups per host (metadata only; admin-only, audit-logged). Off by default |
| `GLASSOPS_NET_AUDIT_MAX_EVENTS` | `200` | Max connection events the agent emits per collect tick (excess dropped, counted) |
| `GLASSOPS_NET_AUDIT_TOP_TALKERS` | `20` | Number of top remote peers kept in each per-minute rollup |
| `GLASSOPS_NET_AUDIT_EVENT_DAYS` | `7` | Retention (days) for raw connection events |
| `GLASSOPS_NET_AUDIT_ROLLUP_DAYS` | `30` | Retention (days) for per-minute traffic rollups |

> Most settings can also be changed at runtime via **Settings > Server** in the web UI without editing `.env`.

## Make Commands

Dashboard host (single-server mode, or the host that runs the UI):

```bash
make up        # Build + start (GPU auto-detected)
make down      # Stop
make logs      # Follow logs
make restart   # Restart
make prod      # Production build (no cache)
make clean     # Stop + remove data
make status    # Show status + agent connection
make shell     # Open shell in container
make help      # Show all commands
```

Remote-host (agent-only — no dashboard, no DB):

```bash
make agent-up        # Start agent container (no GPU)
make agent-up-gpu    # Start agent container with NVIDIA GPU access
make agent-down      # Stop agent container
make agent-logs      # Tail agent logs
```

The agent targets read `agent.env` (copy from `agent.env.example`) and auto-detect the host's docker group GID so the agent can read `/var/run/docker.sock`.

## Metrics History

GlassOps retains up to **7 days** of metrics with automatic downsampling:

| Time Range | Resolution | Storage |
|------------|-----------|---------|
| Last 1 hour | 1 second (raw) | `metrics` table |
| 1h – 24h | 1 minute average | `metrics_downsampled` |
| 1d – 7d | 5 minute average | `metrics_downsampled` |

Data is collected continuously regardless of whether anyone is viewing the dashboard.

## SMTP Email Alerts

Server-side alerts, sent even when nobody is logged in. Configure in
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
IP-resolution checks are skipped for them** — an allowlisted host is fully trusted,
so list only hosts you vouch for. When it is empty, any host that passes the SSRF
checks is allowed: loopback, link-local (including the `169.254.169.254` cloud
metadata address), unspecified, multicast and reserved addresses are blocked, while
RFC1918 private ranges are deliberately allowed so an internal corporate relay works.

### Email critical thresholds

`CPU critical`, `Memory critical` and `Disk critical` (0–100) live on the server and
decide when an email goes out. They are **separate** from the in-browser thresholds
under **Settings > Alerts**, which only drive the desktop toasts and the System
Monitor banner and feed. Both use a `>=` comparison, so the configured value itself
fires.

Only aggregate CPU (`cpu.percent_total`) is evaluated. A single pegged core never
triggers an email — the per-core numbers are a diagnostic display on the Cores tab.

### Saving and testing

**Save & Send Test** saves the form, confirms the save succeeded, and only then sends
through the configuration it just stored. If the save fails, the backend's reason is
shown and no email is sent.

A success message means **the SMTP server accepted the message** — that is not proof
it reached the inbox. Acceptance only says the relay took responsibility for the
message; it can still bounce, be filtered, or be dropped downstream. Confirm in the
recipient mailbox or the provider's delivery log. (The automated suites stub the SMTP
boundary, so they prove the wire format, not delivery. For an end-to-end check against
a real SMTP server — a local sink, asserting on what it actually received — run
`scripts/smtp-sink-check.sh`; it is opt-in and needs a running Docker daemon. See
`scripts/smtp-sink-check.md`.)

The password field shows `********` for a stored credential. Posting that value back
keeps the existing password; typing a new one replaces it. If `GLASSOPS_SECRET_KEY`
changes, the stored ciphertext can no longer be decrypted — the tab says so, sending
is blocked, and the warning clears only once a new password is actually saved.
Removing a stored credential entirely is an API-only operation (`clear_password`);
the UI has no control for it.

### Cooldown

After a successful send, further alerts for **that agent** are suppressed for 5
minutes. The cooldown is per agent, not per resource: while a CPU alert is cooling
down, a new disk alert on the same agent waits too.

A failed send does **not** start that 5-minute cooldown — it backs off for 1 minute
instead, so a transient relay outage delays the next alert by a minute rather than
five, while a persistently dead relay is not retried once per collection tick. The
manual **Save & Send Test** bypasses both, so an admin can always retry immediately.

> **Never commit real SMTP credentials.** There is no environment variable for the
> SMTP username or password — they are set through **Settings > Email** or the admin
> API and stored encrypted. They belong in neither the repository nor a compose file
> checked into it. (`GLASSOPS_SMTP_ALLOWED_HOSTS` is not a credential and is safe to
> commit.)

## Host Monitoring

GlassOps monitors the **host machine**, not just the container:

- `pid: host` — sees all host processes
- `/var/log` mounted — reads host system logs
- `/proc` mounted — collects host CPU/memory/disk metrics
- Docker socket — manages host Docker containers
- `nsenter` — terminal accesses host shell (Linux only)

> On macOS Docker Desktop, some features are limited because Docker runs inside a Linux VM. Process Viewer and Terminal show the Docker VM's processes, not macOS processes.

## Multi-Host Monitoring

By default GlassOps monitors the server it's installed on. To add more hosts, run an agent-only container on each one — the dashboard pulls everything together via the MenuBar dropdown.

### 1. Open the backend port to the LAN

On the dashboard host, expose port `7440` to the network the remote agents live on. Either set `GLASSOPS_BIND` in `.env` (e.g. `GLASSOPS_BIND=10.0.0.9`) or publish on `0.0.0.0` and gate at the firewall, then `make up`.

> Default `127.0.0.1` binding is correct for single-host installs and reverse-proxy setups. Remote agents need direct LAN reachability, not a reverse proxy.

### 2. Install the agent on each remote host

```bash
git clone https://github.com/your-username/glassops.git
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

> **Remote agents must use `wss://`.** The connection carries the agent key and
> RPC commands (including shell/exec on the agent host); over plaintext `ws://`
> anyone on the network path can capture the key and inject commands. Put a TLS
> reverse proxy (Caddy/nginx/Traefik with Let's Encrypt) in front of the dashboard,
> or use a self-signed cert and set `GLASSOPS_AGENT_CA` on each agent.

Then start the agent:

```bash
make agent-up-gpu      # NVIDIA GPU host
# or
make agent-up          # CPU/Docker only
make agent-logs        # tail
```

### 3. Switch hosts in the UI

The MenuBar shows a dropdown once more than one agent is connected. Selecting a host scopes every panel to that host — System Monitor, GPU, Docker (live log streaming included), Logs, Process Viewer all follow the selection.

### What works across hosts

- Real-time metrics (CPU, memory, disk, GPU, network, processes, container list)
- Container start / stop / restart
- Container log streaming (live tail with autoscroll-follow, plus historical date-range queries)
- System log viewer (host log files mounted into the agent)
- Container detail / images / volumes / networks
- Process kill (subject to agent process privileges)

### What doesn't (yet)

- Web terminal — opens a shell on the dashboard host only. Multi-host PTY streaming is a Phase-2 item.

## Production Deployment

### Reverse Proxy (nginx)

GlassOps binds to `127.0.0.1` by default — **a reverse proxy is required for external (Internet / users) access**. Set `GLASSOPS_BIND` to a LAN IP only for direct agent connectivity, not for end-user traffic.

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

    # Required — without this, real-time metrics and terminal won't work
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

> Without the `/ws/` block: System Monitor stays "Connecting...", Terminal shows "Disconnected".

### HTTPS

GlassOps supports httpOnly secure cookies when accessed over HTTPS:

```bash
sudo certbot --nginx -d ops.example.com
```

When HTTPS is active, auth tokens are stored in httpOnly cookies instead of sessionStorage (more secure against XSS).

> **Terminating TLS at an upstream proxy?** The bundled edge speaks plain HTTP internally and deliberately does **not** trust a forwarded `X-Forwarded-Proto` from it (that header is client-spoofable at a loopback edge). So when an external proxy terminates TLS, set `GLASSOPS_FORCE_SECURE_COOKIES=true` so auth cookies still get the `Secure` flag. Without it, cookies are issued without `Secure` even over your HTTPS front end.

### IP Restriction

**Option A — In your reverse proxy** (recommended):
```nginx
allow 10.0.0.0/8;
allow 192.168.0.0/16;
deny all;
```

**Option B — In GlassOps** via Settings > Server or `.env`:
```env
GLASSOPS_ALLOWED_IPS=10.0.0.0/8,192.168.0.0/16
```

> **Behind a reverse proxy?** GlassOps matches the real client IP. If you front it with an LB/TLS proxy, also set `GLASSOPS_TRUSTED_PROXIES` to that proxy's CIDR — otherwise every request looks like it came from the proxy and the whitelist (and per-IP rate limits) misfire. The same list is honored by nginx `real_ip` and the backend, so there is one knob, not two.

### Terminal User

The web terminal runs as a host user, resolved one of three ways:

```env
GLASSOPS_TERMINAL_USER=ubuntu          # a single host user for the local terminal
```

- **Per-user host mappings** (Settings → Users) give each dashboard user a specific
  host account per host.
- `GLASSOPS_ALLOW_LOGIN_PROMPT=true` opts into a host `login` prompt (lets an admin
  authenticate as **any** host account, including root). **Off by default** — with
  none of the above configured, the terminal is refused rather than exposing a login
  prompt to every admin.

The host account's password is still required — GlassOps web login + host password = two-factor access.

### Secret Key

**Always change the default secret key** before deploying:

```bash
GLASSOPS_SECRET_KEY=$(openssl rand -hex 32)
```

This key is used for JWT signing, agent authentication, and SMTP password encryption. Changing it invalidates all sessions and encrypted credentials.

## Security

- JWT authentication (access + refresh tokens with rotation)
- Login rate limiting (5 failures → 5min lockout per IP)
- API rate limiting (100 req/min per IP)
- TOTP 2FA support (Google Authenticator compatible)
- Terminal requires JWT + host user password
- SMTP passwords encrypted at rest (Fernet, derived from SECRET_KEY)
- Docker socket access with auto GID detection
- Environment variable masking in container details
- IP whitelist with self-lockout prevention
- Refresh token blacklist on logout/rotation
- Runtime settings validation (username format, CIDR format, boolean strict)

### Container privileges

The container runs with a **least-privilege capability set instead of
`privileged: true`** — only the capabilities the host terminal (`nsenter` + `su`)
and `process.kill` actually need (`SYS_ADMIN`, `SYS_PTRACE`, `SYS_CHROOT`,
`DAC_READ_SEARCH`, `DAC_OVERRIDE`, `SETUID`, `SETGID`, `KILL`, `CHOWN`); everything
else is dropped. This removes host device access and kernel-module loading. The
host terminal and Docker control are still inherently high-trust operations
(`pid: host` and the Docker socket give broad host reach), so treat dashboard
admin access as equivalent to host root and keep it behind the network controls
above. `apparmor`/`seccomp` are set to `unconfined` because `nsenter` needs to
enter the host namespaces.

> `cgroup: host` in the compose file is **required** for per-container CPU/MEM
> metrics and must not be removed — the `:ro` cgroup mount alone is not enough, as
> the kernel renders `/proc/<pid>/cgroup` paths relative to the reader's namespace.

### Docker socket proxy (optional)

By default the dashboard reaches the host Docker daemon over the mounted
`/var/run/docker.sock`, which is root-equivalent. An opt-in override routes it
through a socket proxy that permits only container **list / inspect / logs / start /
stop / restart** and blocks `create`, `exec`, `build`, `pull`, and `remove`:

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml up -d
```

Every Docker feature in the UI keeps working; only the dangerous write verbs are
denied. This hardens the Docker API path only — the web terminal still enters the
host namespaces, so dashboard admin remains host-root-equivalent. Recommended for
public/multi-tenant deployments; for a trusted single-operator LAN it is optional.

## Contributing

GlassOps uses local and pull-request quality gates for Python, frontend, and
Compose changes. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and commands.

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 18, TypeScript, Vite, zustand, recharts, xterm.js, react-rnd |
| Backend | FastAPI, SQLite (aiosqlite), PyJWT, bcrypt, pyotp, Fernet |
| Agent | psutil, pynvml (GPU), Docker SDK for Python, websockets |
| Infra | Single Docker container, nginx, supervisord |

## License

MIT
