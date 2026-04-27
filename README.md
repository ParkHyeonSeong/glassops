# GlassOps

A macOS-style server monitoring dashboard. All-in-one alternative to Grafana + Portainer — single container, zero configuration.

## Quick Start

```bash
git clone https://github.com/your-username/glassops.git
cd glassops
make up
```

Open **http://localhost:7440** and log in:

| | |
|---|---|
| Email | `admin@glassops.local` |
| Password | `admin` (or check `make logs` for generated password) |

> Change the default password via Settings or set `GLASSOPS_ADMIN_PASSWORD` in `.env` before first run. If unset, a random password is generated and printed to the container logs.

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
| `GLASSOPS_SECRET_KEY` | `change-me-in-production` | JWT signing + SMTP encryption key + remote-agent shared secret (**change this**) |
| `GLASSOPS_ADMIN_EMAIL` | `admin@glassops.local` | Initial admin email |
| `GLASSOPS_ADMIN_PASSWORD` | *(random)* | Initial password (printed to logs if unset) |
| `GLASSOPS_DB_PATH` | `/app/data/glassops.db` | SQLite database path |
| `GLASSOPS_AGENT_ID` | `local` | Agent identifier (this server's own agent) |
| `GLASSOPS_AGENT_KEY` | *(auto)* | Auto-set from SECRET_KEY for the built-in agent. Set explicitly on remote agents to match the backend SECRET_KEY |
| `GLASSOPS_COLLECT_INTERVAL` | `1` | Metrics collection interval (seconds, 1-60) |
| `GLASSOPS_ENABLE_DOCKER` | `true` | Enable Docker container monitoring |
| `GLASSOPS_ENABLE_GPU` | `false` | Enable NVIDIA GPU monitoring (requires pynvml) |
| `GLASSOPS_LOCAL_AGENT_ID` | `local` | Agent ID treated as "local" by the backend. Local-agent REST calls bypass RPC and hit the docker socket directly |
| `GLASSOPS_RPC_TIMEOUT` | `30` | Timeout (s) for backend → agent RPC calls (logs, actions, etc.) |
| `GLASSOPS_TERMINAL_USER` | *(login prompt)* | Host user for web terminal |
| `GLASSOPS_ALLOWED_IPS` | *(all)* | Comma-separated CIDR whitelist |

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

Configure in **Settings > Email**:
- SMTP host, port, credentials (encrypted at rest)
- Recipient email
- Server-side threshold monitoring: alerts are sent even if no one is logged in
- 5-minute cooldown per alert to prevent spam
- Test email button to verify configuration

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
GLASSOPS_AGENT_KEY=<same value as backend SECRET_KEY>
GLASSOPS_SERVER_URL=ws://<dashboard-lan-ip>:7440/ws/agent
GLASSOPS_ENABLE_DOCKER=true
GLASSOPS_ENABLE_GPU=true                             # set false if no NVIDIA GPU
```

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

### Terminal User

By default the web terminal opens a login prompt on the host. To preset a user:

```env
GLASSOPS_TERMINAL_USER=ubuntu
```

The user's password is still required — GlassOps web login + host password = two-factor access.

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

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 18, TypeScript, Vite, zustand, recharts, xterm.js, react-rnd |
| Backend | FastAPI, SQLite (aiosqlite), python-jose (JWT), bcrypt, pyotp, Fernet |
| Agent | psutil, pynvml (GPU), Docker SDK for Python, websockets |
| Infra | Single Docker container, nginx, supervisord |

## License

MIT
