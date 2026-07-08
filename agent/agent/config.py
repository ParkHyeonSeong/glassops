import os

AGENT_ID = os.getenv("GLASSOPS_AGENT_ID", "agent-01")
AGENT_KEY = os.getenv("GLASSOPS_AGENT_KEY", "")
SERVER_URL = os.getenv("GLASSOPS_SERVER_URL", "ws://127.0.0.1:8000/ws/agent")
# TLS for remote (wss://) connections. Cert verification is always on; for a
# self-signed / private CA, point GLASSOPS_AGENT_CA at the CA bundle.
TLS_CA = os.getenv("GLASSOPS_AGENT_CA", "")
REQUIRE_TLS = os.getenv("GLASSOPS_REQUIRE_AGENT_TLS", "false").lower() == "true"
COLLECT_INTERVAL = max(1, int(os.getenv("GLASSOPS_COLLECT_INTERVAL", "1")))
ENABLE_GPU = os.getenv("GLASSOPS_ENABLE_GPU", "false").lower() == "true"
ENABLE_DOCKER = os.getenv("GLASSOPS_ENABLE_DOCKER", "false").lower() == "true"
ENABLE_NET_AUDIT = os.getenv("GLASSOPS_ENABLE_NET_AUDIT", "false").lower() == "true"
NET_AUDIT_MAX_EVENTS = max(1, int(os.getenv("GLASSOPS_NET_AUDIT_MAX_EVENTS", "200")))
NET_AUDIT_TOP_TALKERS = max(1, int(os.getenv("GLASSOPS_NET_AUDIT_TOP_TALKERS", "20")))
