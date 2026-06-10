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

# NVIDIA GPU: copy host libnvidia-ml into container if available via /proc
# privileged mode gives access to host /proc where we can find nvidia libs
NVIDIA_LIB=""
for path in \
  /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 \
  /usr/lib64/libnvidia-ml.so.1 \
  /usr/local/lib/libnvidia-ml.so.1 \
  /usr/lib/libnvidia-ml.so.1; do
  # Check via host proc filesystem
  if [ -f "/host/proc/1/root${path}" ]; then
    cp "/host/proc/1/root${path}" /usr/lib/ 2>/dev/null
    ln -sf /usr/lib/libnvidia-ml.so.1 /usr/lib/libnvidia-ml.so 2>/dev/null
    ldconfig 2>/dev/null
    NVIDIA_LIB="$path"
    break
  fi
done

if [ -n "$NVIDIA_LIB" ]; then
  echo "NVIDIA GPU: loaded driver lib from host ($NVIDIA_LIB)"
else
  echo "NVIDIA GPU: no driver found (GPU monitoring disabled)"
fi

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

# Generate nginx real_ip config: believe X-Forwarded-For ONLY from configured
# upstream proxies (the same trust list the backend uses), so the geo IP whitelist
# and per-IP rate limits key on the TRUE client IP behind a reverse proxy. Unset
# (the bundled nginx is the edge) leaves this comment-only — a no-op.
REALIP_CONF="/etc/nginx/conf.d/real-ip.conf"
echo "# Auto-generated from GLASSOPS_TRUSTED_PROXIES" > "$REALIP_CONF"
REALIP_SET=0
IFS=','
for cidr in ${GLASSOPS_TRUSTED_PROXIES:-}; do
  cidr=$(echo "$cidr" | xargs)  # trim
  case "$cidr" in ""|127.0.0.1*|::1*) continue ;; esac  # skip empty + loopback
  echo "set_real_ip_from $cidr;" >> "$REALIP_CONF"
  REALIP_SET=1
done
unset IFS
if [ "$REALIP_SET" = "1" ]; then
  echo "real_ip_header X-Forwarded-For;" >> "$REALIP_CONF"
  echo "real_ip_recursive on;" >> "$REALIP_CONF"
  echo "nginx real_ip: trusting X-Forwarded-For from $GLASSOPS_TRUSTED_PROXIES"
fi

# Resolve the master secret once before services start (generate/persist if
# unset; refuse to boot if weak) and derive the built-in agent's auth key, so
# the backend and the bundled agent share the exact same values.
GLASSOPS_SECRET_KEY="$(cd /app && python3 -m app.secret_bootstrap secret)" || exit 1
export GLASSOPS_SECRET_KEY
GLASSOPS_AGENT_KEY="$(cd /app && python3 -m app.secret_bootstrap agent)" || exit 1
export GLASSOPS_AGENT_KEY

exec "$@"
