# Contributing to GlassOps

## Prerequisites

- Python 3.12
- Node.js 22
- Docker with Docker Compose v2

These are the versions CI uses (`.github/workflows/ci.yml`) and the pinned base images in
the Dockerfiles.

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

`make dev` starts the same container as production (it needs a `.env`, see the README) with
`backend/app` and `agent/agent` bind-mounted, so the container sees your edits. There is no
automatic reload: after editing backend or agent code, restart the processes:

```bash
docker compose -f docker-compose.dev.yml restart
```

The frontend is compiled into the image, so frontend changes need a rebuild — run `make dev`
again. `make dev-down` stops the dev container.

Most work does not need the container at all: the Python tests run against an in-process
SQLite database, and the frontend tests run under Vitest with jsdom.

## Run the quality gates

```bash
make quality
```

The aggregate target runs the same three gates as CI:

- `make quality-python` — `pip check`, `compileall`, and pytest over `tests/`
- `make quality-frontend` — `npm run quality`: ESLint with `--max-warnings=0`, Vitest, and
  the production build
- `make quality-compose` — Dockerfile base-image pin validation, every supported Compose
  file combination, and entrypoint shell syntax

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
