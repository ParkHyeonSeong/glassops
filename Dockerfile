## ── Stage 1: Build frontend ──────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

## ── Stage 2: Final image ────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl nginx supervisor gosu util-linux \
    procps net-tools iproute2 htop vim-tiny less bash-completion \
    && rm -rf /var/lib/apt/lists/*

ENV TERM=xterm-256color
ENV SHELL=/bin/bash

# Python deps (backend + agent)
COPY backend/requirements.txt /tmp/backend-requirements.txt
COPY agent/requirements.txt /tmp/agent-requirements.txt
RUN pip install --no-cache-dir \
    -r /tmp/backend-requirements.txt \
    -r /tmp/agent-requirements.txt \
    && rm /tmp/*-requirements.txt

# Copy backend
COPY backend/app/ /app/app/

# Copy agent
COPY agent/agent/ /app/agent/

# Copy built frontend
COPY --from=frontend-build /build/dist /app/static

# Nginx config
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default

# Supervisord config
COPY deploy/supervisord.conf /etc/supervisor/conf.d/glassops.conf

# Entrypoint
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create non-root user + data dir
RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

EXPOSE 7440

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/glassops.conf"]
