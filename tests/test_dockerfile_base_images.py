from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts" / "validate-dockerfile-base-images.sh"

NODE_DIGEST = "a" * 64
PYTHON_DIGEST = "b" * 64


def _write_dockerfiles(
    root: Path,
    *,
    frontend_dev_node_digest: str = NODE_DIGEST,
    agent_python_ref: str | None = None,
) -> None:
    files = {
        "Dockerfile": (
            f"FROM node:22-alpine@sha256:{NODE_DIGEST} AS frontend-build\n"
            f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n"
        ),
        "backend/Dockerfile": f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n",
        "agent/Dockerfile": (
            agent_python_ref
            or f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n"
        ),
        "frontend/Dockerfile": (
            f"FROM node:22-alpine@sha256:{NODE_DIGEST} AS build\n"
        ),
        "frontend/Dockerfile.dev": (
            f"FROM node:22-alpine@sha256:{frontend_dev_node_digest}\n"
        ),
    }

    for relative_path, contents in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


def _run_validator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(VALIDATOR), str(root)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_validator_accepts_lockstep_node_and_python_digests(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)

    result = _run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_validator_rejects_node_digest_drift(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path, frontend_dev_node_digest="c" * 64)

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "node:22-alpine digest mismatch" in result.stderr


def test_validator_rejects_unpinned_python_base(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path, agent_python_ref="FROM python:3.12-slim\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "agent/Dockerfile must contain exactly one pinned" in result.stderr
