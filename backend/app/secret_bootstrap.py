"""Master-secret resolution + domain-separated subkey derivation.

Single source of truth shared by the backend (imported in config.py) and the
container entrypoint (`python -m app.secret_bootstrap secret|agent`), so the
shell and Python paths resolve the exact same secret/keys.
"""

import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

# Known-bad / placeholder values that must never sign production tokens.
WEAK = {
    "",
    "dev-secret-key",
    "your-secret-key-here",
    "change-me-in-production",
    "change-me",
    "changeme",
    "secret",
}
MIN_LEN = 32

# Domain-separation labels for derived subkeys.
AGENT_LABEL = "glassops:agent-auth"
SMTP_LABEL = "glassops:smtp-enc"


def resolve_secret(raw: str | None, data_dir: str) -> str:
    """Return a strong master secret, or exit if a weak one was explicitly set.

    - Strong explicit value (>= 32 chars, not a known placeholder) -> use as-is.
    - Explicit but weak/short value -> refuse to boot (SystemExit).
    - Unset -> read/generate a persistent random key at <data_dir>/secret.key.
    """
    raw = (raw or "").strip()
    if raw and raw not in WEAK and len(raw) >= MIN_LEN:
        return raw
    if raw:
        raise SystemExit(
            "FATAL: GLASSOPS_SECRET_KEY is weak or too short "
            f"(min {MIN_LEN} chars, not a placeholder). "
            "Generate a strong one: GLASSOPS_SECRET_KEY=$(openssl rand -hex 32)"
        )

    key_file = Path(data_dir) / "secret.key"
    if key_file.exists():
        stored = key_file.read_text().strip()
        if stored and stored not in WEAK and len(stored) >= MIN_LEN:
            return stored
        raise SystemExit(
            f"FATAL: {key_file} contains a weak or empty key. "
            "Delete it to auto-generate a fresh one, or set GLASSOPS_SECRET_KEY."
        )

    key = secrets.token_hex(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    os.chmod(key_file, 0o600)
    sys.stderr.write(
        f"[glassops] No GLASSOPS_SECRET_KEY set — generated and stored a random key at {key_file}\n"
    )
    return key


def derive_bytes(secret: str, label: str) -> bytes:
    """Domain-separated 32-byte subkey from the master secret."""
    return hmac.new(secret.encode(), label.encode(), hashlib.sha256).digest()


def derive_hex(secret: str, label: str) -> str:
    return derive_bytes(secret, label).hex()


def _data_dir_from_env() -> str:
    db_path = os.getenv("GLASSOPS_DB_PATH", "./data/glassops.db")
    return os.path.dirname(db_path) or "./data"


if __name__ == "__main__":
    # Used by deploy/entrypoint.sh to resolve the secret once before services
    # start, and to print the derived agent key for the built-in agent.
    secret = resolve_secret(os.getenv("GLASSOPS_SECRET_KEY"), _data_dir_from_env())
    what = sys.argv[1] if len(sys.argv) > 1 else "secret"
    if what == "agent":
        print(derive_hex(secret, AGENT_LABEL))
    else:
        print(secret)
