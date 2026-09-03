"""The backend already knows when it stopped storing data; the deployment must ask it.

/ready answers 503 on fail-stop, on close/closing, and on unresolved workers, but
that signal is inert unless two wires exist: the edge must expose /ready, and the
container healthcheck must probe /ready with a command that actually fails on a
503. Missing either one, a fail-stopped container still reports healthy.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = REPO_ROOT / "deploy" / "nginx.conf"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BACKEND_UPSTREAM = "http://127.0.0.1:8000"

# Pinned whole rather than searched for substrings. `curl` without `-f` exits 0 on
# a 503, so a healthcheck that merely mentions /ready still reports healthy while
# the backend says it cannot store data; the CMD exec form keeps out a shell, where
# a trailing `|| true` would swallow the failure just as quietly.
HEALTHCHECK_COMMAND = ["CMD", "curl", "-f", "http://localhost:7440/ready"]


def _nested_block(lines: list[str], key: str, indent: int) -> list[str] | None:
    """The lines nested under `<indent spaces><key>:`, or None if that key is absent."""
    opener = f"{' ' * indent}{key}:"
    for position, line in enumerate(lines):
        if line == opener:
            break
    else:
        return None
    body: list[str] = []
    for line in lines[position + 1 :]:
        if line.strip() and not line.startswith(" " * (indent + 1)):
            break
        body.append(line)
    return body


def _glassops_healthcheck_command() -> list[str]:
    """The exec-form `test:` tokens of the glassops service healthcheck.

    Read by hand instead of with a YAML parser: no YAML parser is declared in
    backend/requirements.txt, agent/requirements.txt, or requirements-dev.txt, and
    a package that merely happens to be installed locally is not a dependency — a
    clean CI would fail collection, not the assertion. The walk is scoped by
    indentation so a `test:` key belonging to some other service, or to a nested
    block, cannot answer for this one.
    """
    lines = COMPOSE_FILE.read_text().splitlines()
    for key, indent in (("services", 0), ("glassops", 2), ("healthcheck", 4)):
        block = _nested_block(lines, key, indent)
        assert block is not None, (
            f"docker-compose.yml has no '{key}:' block at indent {indent}"
        )
        lines = block

    for line in lines:
        if not line.startswith(f"{' ' * 6}test:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise AssertionError(
                f"glassops healthcheck 'test:' is not an exec-form list: {raw!r}"
            ) from None
    raise AssertionError("glassops healthcheck declares no 'test:' command")


def test_nginx_proxies_an_exact_ready_location_to_the_backend() -> None:
    block = re.search(
        r"location\s*=\s*/ready\s*\{(.*?)\}", NGINX_CONF.read_text(), re.DOTALL
    )
    assert block is not None, "deploy/nginx.conf has no exact 'location = /ready' block"
    assert f"proxy_pass {BACKEND_UPSTREAM};" in block.group(1), (
        f"'location = /ready' does not proxy to the backend: {block.group(1)!r}"
    )


def test_compose_healthcheck_pins_the_failing_ready_probe() -> None:
    command = _glassops_healthcheck_command()
    assert command == HEALTHCHECK_COMMAND, (
        "the glassops healthcheck must be exactly this exec-form probe — a dropped "
        "-f, a CMD-SHELL wrapper, or an appended '|| true' each turn a 503 back "
        f"into a healthy container: {command!r}"
    )
