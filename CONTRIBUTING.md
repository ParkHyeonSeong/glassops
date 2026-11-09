# Contributing to GlassOps

## Prerequisites

- Python 3.12
- Node.js 22.13 or newer within the 22.x series
- Docker with Docker Compose v2.24.4+ for the full quality checks (the socket-proxy overlay uses `!override`)

CI and the pinned base images use Python 3.12 and Node.js 22. The frontend lockfile's
jsdom version requires at least Node.js 22.13 on that release line.

## Install development dependencies

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt -r agent/requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
```

## Where things live

| Path | What it is |
|------|------------|
| `backend/app/` | FastAPI backend (`app.main`), the database boundary, and the operator tools (`secret_bootstrap`, `account_state_transfer`, `wal_recovery_cli`) |
| `agent/agent/` | The agent that runs on every monitored host; the same code is bundled into the dashboard image as the local agent |
| `frontend/src/` | React SPA (Vite, TypeScript) |
| `deploy/` | nginx config, supervisord config and the container entrypoint |
| `tests/backend`, `tests/agent` | pytest suites; `tests/*.py` at the top level check image pins and Compose wiring |
| `scripts/` | Dockerfile/Compose validators and the opt-in SMTP end-to-end check |

## Running your changes

Use a separate development checkout with its own `.env` created from `.env.example`.
Run the development stack under a dedicated Compose project and an unused localhost port.
Both Compose files use the `glassops` service and `glassops_data` volume, so using the same
project name would reuse the service and its data. The examples below use `glassops-dev`
and port `7441`. Use the same project name for start, restart, and teardown.

```bash
COMPOSE_PROJECT_NAME=glassops-dev GLASSOPS_PORT=7441 make dev PORT=7441
```

Open `http://localhost:7441`. `make dev` builds from the same dashboard Dockerfile as
production, using the development Compose configuration. The development configuration
bind-mounts `backend/app` and `agent/agent` and omits production's host cgroup namespace
and readiness check. It has no GPU device access. Use a trusted development machine
because the stack mounts the host Docker socket.

Restart after editing backend or agent code to load the mounted changes.

```bash
COMPOSE_PROJECT_NAME=glassops-dev GLASSOPS_PORT=7441 docker compose -f docker-compose.dev.yml restart
```

Frontend changes require an image rebuild. Repeat the `make dev` command above. To stop
the development stack, run the following command.

```bash
COMPOSE_PROJECT_NAME=glassops-dev GLASSOPS_PORT=7441 make dev-down
```

Most work does not need the container at all: the Python tests run against an in-process
SQLite database, and the frontend tests run under Vitest with jsdom.

## Run the quality gates

```bash
make quality
```

The aggregate target runs three local gates that CI also runs.

- `make quality-python` — `pip check`, `compileall`, and pytest over `tests/`
- `make quality-frontend` — `npm run quality`: ESLint with `--max-warnings=0`, Vitest, and
  the production build
- `make quality-compose` — Dockerfile base-image pin validation, every supported Compose
  file combination, and entrypoint shell syntax

The Compose CI job also builds and smoke-checks the dashboard and standalone agent images.

While developing, run the focused command for what you touched, then `make quality` before
opening a pull request:

```bash
python3 -m pytest tests/backend/test_account_state_transfer.py -q
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run typecheck
```

## Docker base images

Base images are pinned by digest in every Dockerfile and kept in lockstep; `make
quality-compose` and CI fail if they drift. To move to newer upstream digests, run
`make refresh-digests` on a development machine (it needs `docker buildx`), review
`git diff`, and commit the result. Do not run it on a deployment host.

## Manual checks that are not in CI

- `scripts/smtp-sink-check.sh` sends a real message through the SMTP code path to a local
  Mailpit sink and asserts on what the sink received. It needs a running Docker daemon and is
  deliberately not part of `make quality`. See `scripts/smtp-sink-check.md`.

## Pull requests

- Keep one behavioral concern per pull request.
- Add or update a regression test for changed behavior.
- Do not disable a quality rule to make a check pass.
- The `Python`, `Frontend`, and `Compose` checks must pass before merge
  (`.github/BRANCH_PROTECTION.md` describes the ruleset).
- CI does not deploy GlassOps or publish images.
