#!/usr/bin/env bash
# Isolated SMTP end-to-end check against a Mailpit sink.
#
# Opt-in and self-contained: a UNIQUELY NAMED Compose project with its own volumes
# and database. It never reads or writes the developer's .env, volumes, or
# alert_config row, so it cannot damage a real SMTP configuration — and because
# every resource is owned by this project, cleanup can never remove someone else's
# container.
#
# The container network is `internal: true`, so nothing running in it can reach the
# outside world and Mailpit is a sink that never relays onward. Build-time traffic
# (image pulls, apt/npm inside the Dockerfile) still uses the host's network — this
# is "no container runtime egress", not "no internet".
#
# What it proves: the stored config is usable, the SMTP transport works, and the
# sink received the expected From / envelope sender / To / Subject / body. What it
# does NOT prove: the metric-triggered aggregate path (check_and_alert) — that is
# covered by tests/backend/test_alert_service.py — or anything about the frontend.
#
# Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED (could not run).
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Unique per run. A fixed name could collide with an existing project, and then
# cleanup would tear down resources this script did not create.
PROJECT="glassops-smtp-check-$$-$(date +%s)"
WORKDIR="$(mktemp -d)"
COMPOSE_FILE="$WORKDIR/docker-compose.check.yml"
BUILD_CTX="$WORKDIR/ctx"
ADMIN_EMAIL="check-admin@example.com"
ADMIN_PASSWORD="check-only-not-a-real-secret-$$"
# Pinned by digest — :latest would make this check non-reproducible. This is an OCI
# multi-arch index and includes linux/arm64. Re-pin with:
#   docker buildx imagetools inspect axllent/mailpit:v1.21.8 --format '{{.Manifest.Digest}}'
MAILPIT_IMAGE="axllent/mailpit:v1.21.8@sha256:81370195cd4a0eab9604d17c2617a7525b0486f9365555253b6c5376c6350f1a"

CURL=(curl -sS --connect-timeout 5 --max-time 20)

blocked() { echo "BLOCKED: $*" >&2; exit 2; }
fail()    { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
  # No `exit` here — the signal traps below set the code. Idempotent, so running
  # once on a signal and again on EXIT is harmless.
  # Always the SAME -f/-p pair used to create things. Without -f, `compose down`
  # would read the repository's own compose files and .env and could stop the
  # developer's real stack. --rmi local also drops the image this run built.
  if [ -f "$COMPOSE_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down -v --rmi local --remove-orphans \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
}
# EXIT carries the real status; the signal traps translate the signal to a non-zero
# code so an interrupted run is never mistaken for a pass (a bare `trap cleanup TERM`
# would let cleanup's own success set exit 0).
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || blocked "docker is not installed"
docker info >/dev/null 2>&1 || blocked "docker daemon is not reachable (start Docker and retry)"
command -v git >/dev/null 2>&1 || blocked "git is not installed"
# An outer wall-clock bound. --wait-timeout only caps the health-wait phase; a hung
# `docker build` (npm/apt inside the Dockerfile) or a stuck image pull happens BEFORE
# that phase and is otherwise unbounded. macOS ships no timeout(1), so require
# timeout/gtimeout and BLOCK if absent rather than run without a ceiling.
TMO="$(command -v timeout || command -v gtimeout || true)"
[ -n "$TMO" ] || blocked "no timeout(1)/gtimeout for an outer wall-clock bound (brew install coreutils)"
case "$MAILPIT_IMAGE" in *PIN_ME*) blocked "MAILPIT_IMAGE digest is not pinned";; esac

# Build context = a clean checkout of HEAD, NOT the working tree. The repo has no
# .dockerignore, so a context of ${REPO_ROOT} would ship .env, data/secret.key, .git
# and ~200MB of node_modules into the build — a real secret-exposure risk, worse with
# a remote Docker context. git archive emits exactly the committed, tracked files, so
# Tasks 1-7 must be COMMITTED for their code to be under test here.
mkdir -p "$BUILD_CTX"
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -x -C "$BUILD_CTX" \
  || blocked "could not export a clean build context from HEAD"

# One Compose file owns BOTH services and the network, so every resource carries the
# project label and `compose down -v --rmi local` removes exactly them and nothing
# else. `internal: true` removes the gateway (no container egress). Host ports are :0
# (ephemeral) so parallel runs cannot collide. Healthchecks let `--wait` bound the
# health phase; the outer `$TMO` bounds build + pull + wait together.
cat > "$COMPOSE_FILE" <<YAML
services:
  mailpit:
    image: ${MAILPIT_IMAGE}
    environment:
      MP_SMTP_BIND_ADDR: 0.0.0.0:2525
    ports: ["127.0.0.1::8025"]
    networks: [sink]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:8025/api/v1/messages"]
      interval: 2s
      timeout: 3s
      retries: 15
  glassops:
    build:
      context: ${BUILD_CTX}
    environment:
      # Injected into the backend PROCESS. A shell-prefixed variable would not reach
      # the service through the repo's normal compose files.
      GLASSOPS_SMTP_ALLOWED_HOSTS: "mailpit"
      GLASSOPS_SECRET_KEY: "smtp-sink-check-secret-key-0123456789abcdef"
      # Setting the admin password explicitly seeds must_change_password=false
      # (database.py: env_pw -> must_change = False), so the inline login below
      # needs no forced-password-change step.
      GLASSOPS_ADMIN_EMAIL: "${ADMIN_EMAIL}"
      GLASSOPS_ADMIN_PASSWORD: "${ADMIN_PASSWORD}"
      # The image's entrypoint only chowns /app/data, so the DB must live there or
      # the unprivileged app user cannot create it.
      GLASSOPS_DB_PATH: "/app/data/check.db"
    volumes:
      - checkdata:/app/data
    ports: ["127.0.0.1::7440"]
    networks: [sink]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:7440/health"]
      interval: 2s
      timeout: 3s
      retries: 30
volumes:
  checkdata:
networks:
  sink:
    internal: true
YAML

# A build/startup failure is a real FAIL (Docker itself is available); so is the
# outer timeout.
"$TMO" 420 docker compose -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build --wait --wait-timeout 300 \
  || fail "the throwaway stack did not build and become healthy within the wall-clock bound"

port_of() { docker compose -f "$COMPOSE_FILE" -p "$PROJECT" port "$1" "$2" 2>/dev/null | tail -1 | sed 's/.*://'; }

# --wait already gated on health; these only resolve the ephemeral host ports.
MAILPIT_PORT="$(port_of mailpit 8025)"; [ -n "$MAILPIT_PORT" ] || fail "no Mailpit host port"
APP_PORT="$(port_of glassops 7440)";    [ -n "$APP_PORT" ]     || fail "no GlassOps host port"
MAILPIT="http://127.0.0.1:${MAILPIT_PORT}"
APP="http://127.0.0.1:${APP_PORT}"

# Inline login. POST /api/auth/login takes {"email","password"} and returns
# access_token (backend/app/routers/auth.py). A login failure is a real FAIL.
LOGIN="$("${CURL[@]}" -X POST "$APP/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" || true)"
TOKEN="$(printf '%s' "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
[ -n "$TOKEN" ] || fail "could not obtain an admin token (login returned: ${LOGIN:0:200})"

post_config() {
  "${CURL[@]}" -o "$WORKDIR/resp.json" -w '%{http_code}' \
    -X POST "$APP/api/alerts/config" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$1"
}

# The allowlist must actually be in effect before anything is sent.
code="$(post_config '{"host":"relay.example.com","port":587,"security":"starttls",
  "from_email":"alerts@example.com","to_email":"ops@example.com"}')"
grep -q "GLASSOPS_SMTP_ALLOWED_HOSTS" "$WORKDIR/resp.json" \
  || fail "allowlist not in effect (HTTP $code): $(cat "$WORKDIR/resp.json")"

code="$(post_config '{"host":"mailpit","port":2525,"security":"none",
  "from_email":"alerts@example.com","to_email":"ops@example.com",
  "thresholds":{"cpu_crit":90,"mem_crit":90,"disk_crit":95}}')"
[ "$code" = "200" ] || fail "saving the sink config returned HTTP $code: $(cat "$WORKDIR/resp.json")"

code="$("${CURL[@]}" -o "$WORKDIR/test.json" -w '%{http_code}' \
  -X POST "$APP/api/alerts/test" -H "Authorization: Bearer $TOKEN")"
[ "$code" = "200" ] || fail "/api/alerts/test returned HTTP $code: $(cat "$WORKDIR/test.json")"

# The API result is not the assertion — what the sink received is. Select the message
# by SUBJECT, not messages[0]: a retry or a stray alert could reorder the inbox, and
# asserting on the wrong message would pass or fail for the wrong reason.
SUBJECT="[GlassOps] Test Alert"
ID=""
for i in $(seq 1 20); do
  "${CURL[@]}" -f "$MAILPIT/api/v1/messages" -o "$WORKDIR/msgs.json" || true
  ID="$(SUBJ="$SUBJECT" python3 -c '
import json, os, sys
try:
    msgs = json.load(open(sys.argv[1]))["messages"]
except Exception:
    sys.exit(0)
for m in msgs:
    if m.get("Subject") == os.environ["SUBJ"]:
        print(m["ID"]); break
' "$WORKDIR/msgs.json" 2>/dev/null || true)"
  [ -n "$ID" ] && break
  [ "$i" -eq 20 ] && fail "no message with subject '$SUBJECT' reached the sink within 20s"
  sleep 1
done

"${CURL[@]}" -f "$MAILPIT/api/v1/message/$ID" -o "$WORKDIR/msg.json" || fail "could not read the message"

SUBJ="$SUBJECT" python3 - "$WORKDIR/msg.json" <<'PY' || fail "the delivered message did not match"
import json, os, sys
m = json.load(open(sys.argv[1]))
frm = m["From"]["Address"]
to = [t["Address"] for t in m["To"]]
body = m.get("Text") or ""
# ReturnPath is the SMTP envelope sender (MAIL FROM). An empty From header produced
# MAIL FROM:<>, so this is the sharpest check that the From-fallback bug stayed fixed.
return_path = (m.get("ReturnPath") or "").strip("<>")
assert frm == "alerts@example.com", f"From header was {frm!r}"
assert return_path == "alerts@example.com", f"envelope sender (ReturnPath) was {return_path!r} — empty means MAIL FROM:<>"
assert to == ["ops@example.com"], f"To was {to!r}"
assert m["Subject"] == os.environ["SUBJ"], f"Subject was {m['Subject']!r}"
assert "test email from GlassOps" in body, f"unexpected body: {body[:120]!r}"
print("sink received:", return_path, "->", to, "|", m["Subject"])
PY

echo "PASS: SMTP end-to-end verified against the isolated sink"
