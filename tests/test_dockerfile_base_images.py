from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts" / "validate-dockerfile-base-images.sh"

NODE_DIGEST = "a" * 64
PYTHON_DIGEST = "b" * 64
NGINX_DIGEST = "c" * 64


def _write_dockerfiles(
    root: Path,
    *,
    frontend_dev_node_digest: str = NODE_DIGEST,
    agent_python_ref: str | None = None,
    frontend_nginx_ref: str | None = None,
    dockerfile_extra_from: str = "",
) -> None:
    frontend_nginx_ref = (
        frontend_nginx_ref or f"FROM nginx:alpine@sha256:{NGINX_DIGEST}\n"
    )
    files = {
        "Dockerfile": (
            f"FROM node:22-alpine@sha256:{NODE_DIGEST} AS frontend-build\n"
            f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n"
            f"{dockerfile_extra_from}"
        ),
        "backend/Dockerfile": f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n",
        "agent/Dockerfile": (
            agent_python_ref
            or f"FROM python:3.12-slim@sha256:{PYTHON_DIGEST}\n"
        ),
        "frontend/Dockerfile": (
            f"FROM node:22-alpine@sha256:{NODE_DIGEST} AS build\n"
            f"{frontend_nginx_ref}"
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


def test_validator_rejects_unpinned_nginx_base_in_existing_dockerfile(
    tmp_path: Path,
) -> None:
    _write_dockerfiles(tmp_path, frontend_nginx_ref="FROM nginx:alpine\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "frontend/Dockerfile: unpinned external FROM nginx:alpine" in result.stderr


def test_validator_rejects_unpinned_additional_base_in_existing_dockerfile(
    tmp_path: Path,
) -> None:
    _write_dockerfiles(tmp_path, dockerfile_extra_from="FROM alpine:latest\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "Dockerfile: unpinned external FROM alpine:latest" in result.stderr


def test_validator_rejects_unpinned_base_in_new_dockerfile(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text("FROM alpine:latest\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile: unpinned external FROM alpine:latest" in result.stderr


def test_validator_rejects_unpinned_base_in_new_dockerfile_variant(
    tmp_path: Path,
) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile.ci"
    experimental.parent.mkdir()
    experimental.write_text("FROM alpine:latest\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile.ci: unpinned external FROM alpine:latest" in result.stderr


def test_validator_rejects_uppercase_digest(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(f"FROM alpine@sha256:{'D' * 64}\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile: unpinned external FROM" in result.stderr


def test_validator_accepts_pinned_platform_base_split_across_lines(
    tmp_path: Path,
) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(
        "FROM --platform=$BUILDPLATFORM \\\n"
        f"  alpine@sha256:{NGINX_DIGEST} AS build\n"
    )

    result = _run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_validator_rejects_63_character_digest(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(f"FROM alpine@sha256:{'d' * 63}\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile: unpinned external FROM" in result.stderr


def test_validator_rejects_65_character_digest(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(f"FROM alpine@sha256:{'d' * 65}\n")

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile: unpinned external FROM" in result.stderr


def test_validator_rejects_alias_used_before_its_declaration(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(
        f"FROM build AS runtime\nFROM alpine@sha256:{NGINX_DIGEST} AS build\n"
    )

    result = _run_validator(tmp_path)

    assert result.returncode != 0
    assert "experimental/Dockerfile: unpinned external FROM build" in result.stderr


def test_validator_accepts_multistage_internal_alias(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text(
        f"FROM alpine@sha256:{NGINX_DIGEST} AS build\nFROM build AS runtime\n"
    )

    result = _run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_validator_accepts_scratch_as_a_non_external_base(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    experimental = tmp_path / "experimental" / "Dockerfile"
    experimental.parent.mkdir()
    experimental.write_text("FROM scratch\n")

    result = _run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_validator_ignores_unmanaged_dockerfile_paths(tmp_path: Path) -> None:
    _write_dockerfiles(tmp_path)
    for relative_path in (
        ".git/Dockerfile",
        "node_modules/package/Dockerfile",
        ".venv/Dockerfile",
        "deploy/contracts/Dockerfile",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM alpine:latest\n")

    result = _run_validator(tmp_path)

    assert result.returncode == 0, result.stderr
