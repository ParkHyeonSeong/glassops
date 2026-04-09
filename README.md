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
| Password | `admin` |

> Change the default password via Settings or set `GLASSOPS_ADMIN_PASSWORD` in `.env` before first run.

## What's Inside

| App | Description |
|-----|-------------|
| System Monitor | Real-time CPU, Memory, Disk, GPU gauges + time-series charts |
| Docker Manager | Container list, Start/Stop/Restart, live logs (Portainer replacement) |
| Network Analyzer | Upload/Download rates, active connections table, interface info |
| Process Viewer | Sortable process table with CPU/MEM bars, search/filter |
| Log Viewer | System logs + Docker container logs, search, auto-refresh |
| Terminal | Web-based terminal (xterm.js), JWT-authenticated, idle timeout |
| Settings | Profile, agent management, appearance |

## Architecture

Single Docker container running 3 internal services via supervisord:

```
┌─────────────────────────────────────────┐
│  GlassOps Container (:7440)             │
│                                         │
│  nginx ─── Frontend (React static)      │
│    │                                    │
│    ├─/api/── Backend (FastAPI)          │
│    ├─/ws/ ── WebSocket (metrics relay)  │
│    │                                    │
│  Agent ──── psutil + Docker SDK         │
│             (collects host metrics)     │
└─────────────────────────────────────────┘
```

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
| `GLASSOPS_SECRET_KEY` | `change-me-in-production` | JWT signing + encryption key (**change this**) |
| `GLASSOPS_ADMIN_EMAIL` | `admin@glassops.local` | Initial admin email |
| `GLASSOPS_ADMIN_PASSWORD` | *(random)* | Initial password (printed to logs if unset) |
| `GLASSOPS_COLLECT_INTERVAL` | `1` | Metrics collection interval (seconds) |
| `GLASSOPS_ENABLE_DOCKER` | `true` | Enable Docker container monitoring |
| `GLASSOPS_ENABLE_GPU` | `false` | Enable NVIDIA GPU monitoring |
| `GLASSOPS_TERMINAL_USER` | *(login prompt)* | Host user for web terminal |
| `GLASSOPS_ALLOWED_IPS` | *(all)* | Comma-separated IP whitelist |

## Make Commands

```bash
make up        # Build + start
make down      # Stop
make logs      # Follow logs
make restart   # Restart
make prod      # Production build (no cache)
make clean     # Stop + remove data
make status    # Show status + agent connection
make help      # Show all commands
```

## Host Monitoring

GlassOps monitors the **host machine**, not just the container:

- `pid: host` — sees all host processes
- `/var/log` mounted — reads host system logs
- `/proc` mounted — collects host CPU/memory/disk metrics
- Docker socket — manages host Docker containers
- `nsenter` — terminal accesses host shell (Linux only)

> On macOS Docker Desktop, some features are limited because Docker runs inside a Linux VM.

## Remote Agents

By default, GlassOps monitors the server it's installed on. To monitor additional servers:

1. Install the agent on the remote server:
   ```bash
   cd agent && pip install -r requirements.txt
   GLASSOPS_SERVER_URL=ws://your-glassops-host:7440/ws/agent \
   GLASSOPS_AGENT_KEY=your-key \
   python -m agent.main
   ```

2. The remote server's metrics will appear in the dashboard alongside the local server.

## Production Deployment

### Reverse Proxy (nginx)

If you run GlassOps behind a reverse proxy (recommended), add WebSocket support:

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

GlassOps supports httpOnly secure cookies when accessed over HTTPS. Use certbot or your preferred method:

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

**Option B — In GlassOps** via `.env`:
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

```env
GLASSOPS_SECRET_KEY=$(openssl rand -hex 32)
```

This key is used for JWT signing and SMTP password encryption. Changing it invalidates all sessions and encrypted credentials.

## Security

- JWT authentication (access + refresh tokens with rotation)
- Login rate limiting (5 failures → 5min lockout)
- API rate limiting (100 req/min per IP)
- TOTP 2FA support (Google Authenticator compatible)
- Terminal requires JWT + host user password
- SMTP passwords encrypted at rest (Fernet/AES)
- Docker socket access with auto GID detection
- Environment variable masking in container details
- IP whitelist support (nginx-level)
- Refresh token blacklist on logout/rotation

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 18, TypeScript, Vite, zustand, recharts, xterm.js, react-rnd |
| Backend | FastAPI, SQLite (aiosqlite), python-jose (JWT), bcrypt, pyotp |
| Agent | psutil, Docker SDK for Python, websockets |
| Infra | Single Docker container, nginx, supervisord |

## License

MIT
