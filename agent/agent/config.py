import os

AGENT_ID = os.getenv("GLASSOPS_AGENT_ID", "agent-01")
AGENT_KEY = os.getenv("GLASSOPS_AGENT_KEY", "")
SERVER_URL = os.getenv("GLASSOPS_SERVER_URL", "ws://127.0.0.1:8000/ws/agent")
COLLECT_INTERVAL = max(1, int(os.getenv("GLASSOPS_COLLECT_INTERVAL", "1")))
ENABLE_GPU = os.getenv("GLASSOPS_ENABLE_GPU", "false").lower() == "true"
ENABLE_DOCKER = os.getenv("GLASSOPS_ENABLE_DOCKER", "false").lower() == "true"
