FROM python:3.12-slim

WORKDIR /app

# util-linux: nsenter (host PTY for terminal). procps/login: shell + login facilities.
RUN apt-get update && apt-get install -y --no-install-recommends \
        util-linux procps login \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

# Runs as root so the agent can spawn host PTYs via nsenter --target 1.
# The container is already started with --privileged, so this matches the existing trust model.
CMD ["python", "-m", "agent.main"]
