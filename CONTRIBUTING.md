# Contributing to GlassOps

## Prerequisites

- Python 3.12
- Node.js 22
- Docker with Docker Compose v2

## Install development dependencies

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt -r agent/requirements.txt -r requirements-dev.txt
npm ci --prefix frontend
```

## Run the quality gates

```bash
make quality
```

The aggregate command runs:

- `make quality-python`: dependency integrity, compile checks, and pytest
- `make quality-frontend`: strict ESLint, Vitest, and the production frontend build
- `make quality-compose`: supported Compose combinations and shell syntax

Run the relevant focused target while developing, then run `make quality` before opening a pull request.

## Pull requests

- Keep one behavioral concern per pull request.
- Add or update a regression test for changed behavior.
- Do not disable a quality rule to make a check pass.
- `Python`, `Frontend`, and `Compose` must pass before merge.
- CI does not deploy GlassOps or publish images.
