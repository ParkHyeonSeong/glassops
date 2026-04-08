#!/bin/sh
# Auto-detect docker socket GID and grant appuser access
if [ -S /var/run/docker.sock ]; then
  SOCK_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock 2>/dev/null)
  if [ -n "$SOCK_GID" ] && [ "$SOCK_GID" != "0" ]; then
    getent group "$SOCK_GID" >/dev/null 2>&1 || addgroup --gid "$SOCK_GID" dockerhost 2>/dev/null
    GRP_NAME=$(getent group "$SOCK_GID" | cut -d: -f1)
    adduser appuser "$GRP_NAME" 2>/dev/null
  else
    adduser appuser root 2>/dev/null
  fi
fi

# Ensure data directory
mkdir -p /app/data
chown appuser:appuser /app/data

exec "$@"
