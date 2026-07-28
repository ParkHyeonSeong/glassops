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
# Nothing is published to the host: `internal: true` and port publishing are mutually
# exclusive (Docker maps no host port on an internal network, and `compose port` then
# answers "invalid IP:0"). Every HTTP call therefore runs INSIDE the network via
# `docker compose exec`, which keeps the isolation intact rather than trading it for
# reachability.
#
# What it proves: the stored config is usable, the SMTP transport works, and the
# sink received the expected From / envelope sender / To / Subject / body. What it
# does NOT prove: the metric-triggered aggregate path (check_and_alert) — that is
# covered by tests/backend/test_alert_service.py — or anything about the frontend.
#
# Exit codes on normal completion: 0 = PASS, 1 = FAIL, 2 = BLOCKED (could not run).
# Nothing else: every command that can fail is guarded so a raw curl/docker status
# never escapes. Interrupted by a signal, the traps below return 128+signo
# (129 HUP / 130 INT / 143 TERM) so an interrupted run is never mistaken for a pass.
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

# Every docker invocation runs under this. --kill-after escalates to SIGKILL for a
# child that ignores TERM, so no docker call is unbounded.
DOCKER_CALL_TIMEOUT=60
CLEANUP_TIMEOUT=120
STACK_TIMEOUT=420
# Wall-clock deadline for the sink polling loop. Iteration count alone is not a time
# bound: 20 iterations x a 20s curl is ~7 minutes, not 20 seconds.
SINK_DEADLINE=60

CLEANUP_FAILED=0
# idle -> running -> done. Marking "done" only after the teardown returns means an
# interrupted teardown is re-entered (and its recovery guidance printed) rather than
# skipped as already handled.
CLEANUP_STATE=idle

blocked() { echo "BLOCKED: $*" >&2; exit 2; }
fail()    { echo "FAIL: $*" >&2; exit 1; }

# Bounded docker wrapper. $TMO is resolved during pre-flight, below.
dk() { "$TMO" --kill-after=10 "$DOCKER_CALL_TIMEOUT" docker "$@"; }

cleanup() {
  local rc=$?
  # `done` short-circuits the second entry that the explicit call at the end of a
  # passing run would otherwise cause (it exits 1 on teardown failure, re-entering
  # through the EXIT trap). `running` is NOT treated as handled: if a signal
  # interrupts the teardown, the EXIT trap must still report what was left behind.
  [ "$CLEANUP_STATE" = done ] && return "$rc"
  CLEANUP_STATE=running
  # Always the SAME -f/-p pair used to create things. Without -f, `compose down`
  # would read the repository's own compose files and .env and could stop the
  # developer's real stack. --rmi local also drops the image this run built.
  if [ -f "$COMPOSE_FILE" ]; then
    if ! "$TMO" --kill-after=10 "$CLEANUP_TIMEOUT" \
        docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down -v --rmi local --remove-orphans \
        >/dev/null 2>&1; then
      CLEANUP_FAILED=1
      CLEANUP_STATE=done
      # Keep the compose file: without it the leftovers cannot be torn down with the
      # same -f/-p pair, and a bare `docker rm` could hit another project.
      echo "WARNING: cleanup failed — resources may remain for project '$PROJECT'." >&2
      echo "         Remove them with:" >&2
      echo "         docker compose -f $COMPOSE_FILE -p $PROJECT down -v --rmi local --remove-orphans" >&2
      # A successful run whose cleanup failed is not a clean pass. A run that was
      # already failing (or was signalled) keeps its own status — 1 must not
      # overwrite 143.
      [ "$rc" -eq 0 ] && exit 1
      return "$rc"
    fi
  fi
  CLEANUP_STATE=done
  rm -rf "$WORKDIR"
  return "$rc"
}
# EXIT carries the real status; the signal traps translate the signal to a non-zero
# code so an interrupted run is never mistaken for a pass (a bare `trap cleanup TERM`
# would let cleanup's own success set the status).
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- pre-flight: everything missing here is BLOCKED, never FAIL --------------
for tool in docker git curl python3; do
  command -v "$tool" >/dev/null 2>&1 || blocked "$tool is not installed"
done
# An outer wall-clock bound. Compose's --wait-timeout only caps the health-wait
# phase; a hung `docker build` (npm/apt inside the Dockerfile) or a stuck image pull
# happens BEFORE that phase and is otherwise unbounded. macOS ships no timeout(1), so
# require timeout/gtimeout and BLOCK if absent rather than run without a ceiling.
TMO="$(command -v timeout || command -v gtimeout || true)"
[ -n "$TMO" ] || blocked "no timeout(1)/gtimeout for an outer wall-clock bound (brew install coreutils)"
dk compose version >/dev/null 2>&1 || blocked "docker compose (v2) is not available"
dk info >/dev/null 2>&1 || blocked "docker daemon is not reachable (start Docker and retry)"
case "$MAILPIT_IMAGE" in *PIN_ME*) blocked "MAILPIT_IMAGE digest is not pinned";; esac

# Build context = a clean checkout of HEAD, NOT the working tree. The repo has no
# .dockerignore, so a context of ${REPO_ROOT} would ship .env, data/secret.key, .git
# and ~200MB of node_modules into the build — a real secret-exposure risk, worse with
# a remote Docker context. git archive emits exactly the committed, tracked files, so
# the work under test must be COMMITTED.
mkdir -p "$BUILD_CTX"
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -x -C "$BUILD_CTX" \
  || blocked "could not export a clean build context from HEAD"

# One Compose file owns BOTH services and the network, so every resource carries the
# project label and `compose down -v --rmi local` removes exactly them and nothing
# else. `internal: true` removes the gateway (no container egress). Host ports are :0
# (ephemeral) so parallel runs cannot collide. Mailpit ships its own `readyz` probe;
# --wait bounds the health phase and the outer $TMO bounds build + pull + wait.
cat > "$COMPOSE_FILE" <<YAML
services:
  mailpit:
    image: ${MAILPIT_IMAGE}
    environment:
      MP_SMTP_BIND_ADDR: 0.0.0.0:2525
    networks: [sink]
    healthcheck:
      test: ["CMD", "/mailpit", "readyz"]
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
"$TMO" --kill-after=30 "$STACK_TIMEOUT" \
  docker compose -f "$COMPOSE_FILE" -p "$PROJECT" up -d --build --wait --wait-timeout 300 \
  || fail "the throwaway stack did not build and become healthy within ${STACK_TIMEOUT}s"

# All HTTP runs inside the network (see the header): the app container has curl, so
# it drives both the GlassOps API and the Mailpit API by service name. `|| true` plus
# an explicit status check keeps a failed exec from aborting with docker's own status
# instead of the documented FAIL=1.
MAILPIT="http://mailpit:8025"
APP="http://127.0.0.1:7440"

# $1 = output file on the HOST, rest = curl args run in the container.
in_app() {
  local out="$1"; shift
  dk compose -f "$COMPOSE_FILE" -p "$PROJECT" exec -T glassops \
    curl -sS --connect-timeout 5 --max-time 20 "$@" > "$out" 2>/dev/null || true
}
# Same, but returns only the HTTP status on stdout and writes the body to $1.
in_app_status() {
  local out="$1"; shift
  local body_and_code
  body_and_code="$(dk compose -f "$COMPOSE_FILE" -p "$PROJECT" exec -T glassops \
    curl -sS --connect-timeout 5 --max-time 20 -w '\n%{http_code}' "$@" 2>/dev/null || true)"
  printf '%s' "$body_and_code" | sed '$d' > "$out"
  printf '%s' "$body_and_code" | tail -1
}

# Inline login. POST /api/auth/login takes {"email","password"} and returns
# access_token (backend/app/routers/auth.py). The response body is NEVER echoed: it
# carries the access and refresh tokens.
LOGIN_STATUS="$(in_app_status "$WORKDIR/login.json" \
  -X POST "$APP/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}")"
[ "$LOGIN_STATUS" = "200" ] || fail "login returned HTTP ${LOGIN_STATUS:-<no response>}"
TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("access_token",""))' \
  "$WORKDIR/login.json" 2>/dev/null || true)"
[ -n "$TOKEN" ] || fail "login succeeded but no access_token was returned (body not shown: it holds tokens)"

post_config() {
  in_app_status "$WORKDIR/resp.json" -X POST "$APP/api/alerts/config" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$1"
}

# The allowlist must actually be in effect before anything is sent. Assert the status
# AND the exact detail: a 500 whose body merely mentions the variable would otherwise
# read as a pass.
code="$(post_config '{"host":"relay.example.com","port":587,"security":"starttls",
  "from_email":"alerts@example.com","to_email":"ops@example.com"}')"
[ "$code" = "400" ] || fail "expected HTTP 400 for a non-allowlisted host, got ${code:-<no response>}"
detail="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("detail",""))' \
  "$WORKDIR/resp.json" 2>/dev/null || true)"
[ "$detail" = "SMTP host is not in GLASSOPS_SMTP_ALLOWED_HOSTS" ] \
  || fail "allowlist not in effect — detail was: ${detail:-<unparseable>}"

code="$(post_config '{"host":"mailpit","port":2525,"security":"none",
  "from_email":"alerts@example.com","to_email":"ops@example.com",
  "thresholds":{"cpu_crit":90,"mem_crit":90,"disk_crit":95}}')"
[ "$code" = "200" ] || fail "saving the sink config returned HTTP ${code:-<no response>}"

code="$(in_app_status "$WORKDIR/test.json" \
  -X POST "$APP/api/alerts/test" -H "Authorization: Bearer $TOKEN")"
[ "$code" = "200" ] || fail "/api/alerts/test returned HTTP ${code:-<no response>}"

# The API result is not the assertion — what the sink received is. Select the message
# by SUBJECT, not messages[0]: a retry or a stray alert could reorder the inbox, and
# asserting on the wrong message would pass or fail for the wrong reason. The loop is
# bounded by wall-clock, not by iteration count.
SUBJECT="[GlassOps] Test Alert"
ID=""
# Fractional monotonic deadline. Integer $SECONDS plus an unconditional `sleep 1`
# would still overshoot by up to a second; both the request and the sleep are capped
# by the time actually left, so the phase is a hard bound.
now() { python3 -c 'import time; print(time.monotonic())'; }
sink_deadline="$(python3 -c "import time; print(time.monotonic() + $SINK_DEADLINE)")"
while [ -z "$ID" ]; do
  remaining="$(python3 -c "print(max(0.0, $sink_deadline - $(now)))")"
  [ "$(python3 -c "print(1 if $remaining <= 0.05 else 0)")" = "1" ] \
    && fail "no message with subject '$SUBJECT' reached the sink within ${SINK_DEADLINE}s"
  dk compose -f "$COMPOSE_FILE" -p "$PROJECT" exec -T glassops \
    curl -sS --connect-timeout 5 --max-time "$remaining" -f "$MAILPIT/api/v1/messages" \
    > "$WORKDIR/msgs.json" 2>/dev/null || true
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
  # Sleep only as long as the deadline allows (at most 1s).
  nap="$(python3 -c "print(min(1.0, max(0.0, $sink_deadline - $(now))))")"
  [ "$(python3 -c "print(1 if $nap <= 0.0 else 0)")" = "1" ] \
    && fail "no message with subject '$SUBJECT' reached the sink within ${SINK_DEADLINE}s"
  sleep "$nap"
done

in_app "$WORKDIR/msg.json" -f "$MAILPIT/api/v1/message/$ID"
[ -s "$WORKDIR/msg.json" ] || fail "could not read the message from the sink"

# Explicit checks, not `assert`: python -O / PYTHONOPTIMIZE=1 strips assert
# statements outright, which would make every one of these pass on a wrong message.
SUBJ="$SUBJECT" python3 - "$WORKDIR/msg.json" <<'PY' || fail "the delivered message did not match"
import json, os, sys

problems = []
try:
    m = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"could not parse the message: {type(e).__name__}", file=sys.stderr)
    raise SystemExit(1)

frm = (m.get("From") or {}).get("Address", "")
to = [t.get("Address", "") for t in (m.get("To") or [])]
body = m.get("Text") or ""
# ReturnPath is the SMTP envelope sender (MAIL FROM). An empty From header produced
# MAIL FROM:<>, so this is the sharpest check that the From-fallback bug stayed fixed.
return_path = (m.get("ReturnPath") or "").strip("<>")
subject = m.get("Subject", "")

if frm != "alerts@example.com":
    problems.append(f"From header was {frm!r}")
if return_path != "alerts@example.com":
    problems.append(f"envelope sender (ReturnPath) was {return_path!r} — empty means MAIL FROM:<>")
if to != ["ops@example.com"]:
    problems.append(f"To was {to!r}")
if subject != os.environ["SUBJ"]:
    problems.append(f"Subject was {subject!r}")
if "test email from GlassOps" not in body:
    problems.append(f"unexpected body: {body[:120]!r}")

if problems:
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)

print("sink received:", return_path, "->", to, "|", subject)
PY

# Do NOT print PASS yet: cleanup runs on EXIT, and leftover containers/networks/
# volumes must not be reported as a clean run. cleanup() turns a failed teardown into
# exit 1, so the caller's status is the truth either way.
echo "verified: the sink received the expected message; tearing down..."
cleanup
trap - EXIT
[ "$CLEANUP_FAILED" -eq 0 ] || fail "the check passed but cleanup left resources behind"
echo "PASS: SMTP end-to-end verified against the isolated sink"
