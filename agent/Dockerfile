# Digest-pinned (run `make refresh-digests` to update). Highest-value pin: this image
# runs as root with nsenter --target 1 on every monitored host.
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

WORKDIR /app

# util-linux: nsenter (host PTY for terminal). procps/login: shell + login facilities.
RUN apt-get update && apt-get install -y --no-install-recommends \
        util-linux procps login \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

# Runs as root so the agent can spawn host PTYs via nsenter --target 1.
# The container is already started with --privileged, so this matches the existing trust model.
CMD ["python", "-m", "agent.main"]
