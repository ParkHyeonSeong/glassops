#!/usr/bin/env bash
# Acceptance check against disposable containers/volume only. No host mounts.
set -euo pipefail
image=${1:?Usage: check-runtime-log-retention.sh IMAGE}
work=$(mktemp -d)
name="glassops-retention-check-$$-$(date +%s)"
volume="${name}-data"
container="${name}-a"
follower=""
cleanup() {
  code=$?
  if [ "$code" -ne 0 ]; then
    docker logs "$container" 2>/dev/null || true
    docker exec "$container" python -m app.runtime_logs read --tail 60 2>/dev/null || true
  fi
  if [ -n "$follower" ]; then kill "$follower" 2>/dev/null || true; wait "$follower" 2>/dev/null || true; fi
  docker rm -f "${name}-a" "${name}-b" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$code"
}
trap cleanup EXIT

docker volume create "$volume" >/dev/null
start() {
  docker run -d --name "$container" --cap-drop FOWNER --cap-add SYS_PTRACE \
    --mount "type=volume,source=$volume,target=/app/data" \
    --env GLASSOPS_SECRET_KEY=retention-test-only-secret-0123456789abcdef \
    --env GLASSOPS_ENABLE_GPU=false --env GLASSOPS_ENABLE_DOCKER=false \
    --env GLASSOPS_ENABLE_NET_AUDIT=false "$image" >/dev/null
  for _ in $(seq 1 60); do
    if docker exec "$container" curl -fsS http://127.0.0.1:7440/ready >/dev/null 2>&1; then
      docker exec "$container" curl -fsS http://127.0.0.1:7440/health >/dev/null
      sleep 3 # Supervisor's agent startsecs; do not rotate a startup wrapper.
      return
    fi
    sleep 1
  done
  echo "Dashboard did not become ready" >&2
  return 1
}
start

docker exec -i "$container" python - <<'PY'
from pathlib import Path
import os, pwd, subprocess, tempfile, time
from app.runtime_logs import prepare_runtime_logs
# Compose does not grant FOWNER. nginx owns its empty error file, and an
# older dashboard can have chowned the archive directory to appuser.
with tempfile.TemporaryDirectory() as directory:
    quiet = Path(directory)
    prepare_runtime_logs(quiet)
    os.chown(quiet / 'nginx-error.log', pwd.getpwnam('www-data').pw_uid, -1)
    prepare_runtime_logs(quiet)
    assert (quiet / 'nginx-error.log').stat().st_uid == 0
    assert (quiet / 'nginx-error.log').read_bytes() == b''
    (quiet / 'backend.log').write_text('PRESERVE-OWNERSHIP-RECOVERY')
    os.chown(quiet, pwd.getpwnam('appuser').pw_uid, -1)
    prepare_runtime_logs(quiet)
    assert quiet.stat().st_uid == 0
    assert any('PRESERVE-OWNERSHIP-RECOVERY' in p.read_text() for p in quiet.glob('backend.*.log'))
root = Path('/app/data/logs')
assert any('NVIDIA GPU:' in p.read_text() for p in root.glob('bootstrap*.log'))
assert any('spawned:' in p.read_text() for p in root.glob('supervisor*.log'))
# Successful nginx configuration validation is deliberately silent.
assert any('/ready' in p.read_text() for p in root.glob('nginx-access*.log'))
assert (root / 'nginx-error.log').exists()
assert all('[emerg]' not in p.read_text() for p in root.glob('nginx-error*.log'))
for program in ('backend', 'agent'):
    pid = subprocess.check_output(['supervisorctl', '-c', '/etc/supervisor/conf.d/glassops.conf', 'pid', program], text=True).strip()
    with open(f'/proc/{pid}/fd/1', 'w') as output:
        output.write(f'RETENTION-BEFORE-{program}\n')
time.sleep(.2)
PY

docker exec "$container" python -m app.runtime_logs follow --tail 1 >"$work/follow.log" 2>&1 &
follower=$!
docker exec -i "$container" python - <<'PY'
from pathlib import Path
import os, subprocess, threading, time
from app.runtime_logs import ROOT, _maintain_once
programs = ('backend', 'nginx', 'agent')
def pids():
    return {p: subprocess.check_output(['supervisorctl', '-c', '/etc/supervisor/conf.d/glassops.conf', 'pid', p], text=True).strip() for p in programs}
before = pids()
def emit():
    with open(f'/proc/{before["backend"]}/fd/1', 'w', buffering=1) as output:
        for i in range(30):
            output.write(f'RETENTION-DURING-{i:02d}\n')
            time.sleep(.03)
producer = threading.Thread(target=emit)
producer.start()
_maintain_once()
producer.join()
assert pids() == before, 'Rotation restarted a service'
with open(f'/proc/{before["backend"]}/fd/1', 'w') as output:
    output.write('RETENTION-AFTER-ROTATION\n')
subprocess.run(['curl', '-fsS', 'http://127.0.0.1:7440/health?RETENTION-NGINX'], check=True, stdout=subprocess.DEVNULL)
time.sleep(.3)
body = '\n'.join(p.read_text() for p in ROOT.glob('*.log'))
assert '[emerg]' not in body, 'nginx could not reopen a log'
for token in ('RETENTION-BEFORE-backend', 'RETENTION-BEFORE-agent', 'RETENTION-AFTER-ROTATION', 'RETENTION-NGINX'):
    assert token in body, token
for i in range(30):
    assert f'RETENTION-DURING-{i:02d}' in body, f'missing marker {i}'
# Wholly expired files are hidden even before physical cleanup catches up.
from app.runtime_logs import read_runtime_logs, prune_runtime_logs, RETENTION_SECONDS
old = ROOT / 'backend.20000101T000000000000Z.111111111111.log'
recent = ROOT / 'backend.20000101T000000000000Z.222222222222.log'
now = time.time()
for path, age, text in ((old, RETENTION_SECONDS + 60, 'EXPIRED-TEST'),
                        (recent, RETENTION_SECONDS - 60, 'RETAINED-TEST')):
    path.write_text(text)
    os.utime(path, (now-age, now-age))
assert 'EXPIRED-TEST' not in str(read_runtime_logs(ROOT, tail=5000))
assert old.exists()
prune_runtime_logs(ROOT, now)
assert not old.exists() and recent.exists()
PY
for _ in $(seq 1 15); do
  if grep -q RETENTION-AFTER-ROTATION "$work/follow.log"; then break; fi
  sleep 1
done
grep -q RETENTION-AFTER-ROTATION "$work/follow.log"
kill "$follower" 2>/dev/null || true
wait "$follower" 2>/dev/null || true
follower=""

docker stop --time 30 "$container" >/dev/null
docker rm "$container" >/dev/null
container="${name}-b"
start
docker exec --user appuser "$container" python -m app.runtime_logs read --tail 5000 >"$work/after.log"
grep -q RETENTION-BEFORE-backend "$work/after.log"
grep -q RETENTION-BEFORE-agent "$work/after.log"
grep -q RETENTION-AFTER-ROTATION "$work/after.log"
# Recovery reader runs with a read-only mount, without entrypoint/DB bootstrap.
docker run --rm --entrypoint python \
  --mount "type=volume,source=$volume,target=/app/data,readonly" \
  "$image" -m app.runtime_logs read --tail 5000 >"$work/recovery.log"
grep -q RETENTION-BEFORE-backend "$work/recovery.log"
echo "PASS: log routing, rotation without restart/loss, expiry, follow, recreation and read-only recovery"
