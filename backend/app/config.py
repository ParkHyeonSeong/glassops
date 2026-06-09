import os

from pydantic_settings import BaseSettings

from app.secret_bootstrap import (
    AGENT_LABEL,
    SMTP_LABEL,
    derive_bytes,
    derive_hex,
    resolve_secret,
)


class Settings(BaseSettings):
    secret_key: str = ""          # no hardcoded default — resolved below
    agent_key: str = ""           # empty -> derived from secret_key
    db_path: str = "./data/glassops.db"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    local_agent_id: str = "local"
    rpc_timeout: int = 30
    # Reverse proxies whose forwarded headers (X-Real-IP / X-Forwarded-Proto) we
    # trust. Default = the bundled nginx (loopback). Add upstream LB/proxy IPs here.
    trusted_proxies: str = "127.0.0.1,::1"
    force_secure_cookies: bool = False   # set true when TLS is terminated upstream
    # CSWSH guard: browser origins permitted on WebSockets (comma-separated, e.g.
    # "https://ops.lan,http://10.0.0.9:7440"). Empty = match the request Host (same-host LAN).
    allowed_origins: str = ""
    # Restrict which SMTP relay hosts the alert config may use (comma-separated).
    # Empty = any host that passes the SSRF checks (loopback/link-local/metadata blocked).
    smtp_allowed_hosts: str = ""

    model_config = {"env_prefix": "GLASSOPS_"}


settings = Settings()

# Resolve the master secret once at import (refuses to boot on a weak value,
# auto-generates a persistent one when unset).
_data_dir = os.path.dirname(settings.db_path) or "./data"
settings.secret_key = resolve_secret(settings.secret_key, _data_dir)

# Agent auth key is domain-separated from the JWT signing secret so that handing
# it to a remote agent never reveals the signing secret.
if not settings.agent_key:
    settings.agent_key = derive_hex(settings.secret_key, AGENT_LABEL)


def smtp_fernet_key() -> bytes:
    """32-byte key for encrypting stored SMTP credentials (domain-separated)."""
    return derive_bytes(settings.secret_key, SMTP_LABEL)
