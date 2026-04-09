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

# Generate IP whitelist nginx config
ALLOWED_IPS="${GLASSOPS_ALLOWED_IPS:-}"
IP_CONF="/etc/nginx/conf.d/ip-whitelist.conf"

if [ -n "$ALLOWED_IPS" ]; then
  echo "# Auto-generated IP whitelist" > "$IP_CONF"
  echo "geo \$ip_whitelist {" >> "$IP_CONF"
  echo "  default 0;" >> "$IP_CONF"
  echo "  127.0.0.1 1;" >> "$IP_CONF"
  echo "  ::1 1;" >> "$IP_CONF"
  IFS=','
  for ip in $ALLOWED_IPS; do
    ip=$(echo "$ip" | xargs)  # trim
    [ -n "$ip" ] && echo "  $ip 1;" >> "$IP_CONF"
  done
  echo "}" >> "$IP_CONF"
  echo "IP whitelist enabled: 127.0.0.1, $ALLOWED_IPS"
else
  # No whitelist — allow all
  echo "geo \$ip_whitelist { default 1; }" > "$IP_CONF"
fi

exec "$@"
