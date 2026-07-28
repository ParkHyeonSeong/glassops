# Isolated SMTP end-to-end check

`scripts/smtp-sink-check.sh` sends a real message through the real SMTP code path to
a local [Mailpit](https://mailpit.axllent.org/) sink and asserts on **what the sink
received**, not on the API returning 200.

Manual and opt-in. It is deliberately **not** wired into `make quality`, CI, or any
compose file — it needs a Docker daemon and it builds an image, neither of which
belongs in the unit-test gate.

## Running it

```bash
# Preserve the exit code — `script && echo PASS || echo FAIL` resolves to 0 either way.
( ./scripts/smtp-sink-check.sh; rc=$?; \
  [ "$rc" -eq 0 ] && echo "E2E: PASS" || echo "E2E: exit=$rc"; exit "$rc" )
```

| Exit | Meaning |
|---|---|
| `0` | PASS — the sink received the expected message |
| `1` | FAIL — build, startup, login, config save, send, or an assertion failed |
| `2` | BLOCKED — could not run at all |

**BLOCKED is not a pass.** It means the SMTP path was never exercised, so the unit
suites must not be reported as covering it. The causes are: a missing `docker`,
`git` or `python3`; no `timeout`/`gtimeout`; no Docker Compose v2; an unreachable
Docker daemon; or an unpinned Mailpit digest. Host `curl` is *not* among them — see
Prerequisites. Everything else — build, startup, login, config save, send, an
assertion, or a failed teardown — is FAIL (1).

Exit codes are normalised: every `curl`, `docker` and `docker compose` call is
guarded, so a raw status like curl's `7` (connection refused) or `28` (timeout) can
never escape as the script's exit code.

### Prerequisites

- A **running** Docker daemon (`docker info` must succeed — installing Docker is not
  enough). Host `curl` is *not* required: every HTTP call runs inside the app
  container, which ships its own.
- `timeout` or `gtimeout` on `PATH`. macOS ships neither; `brew install coreutils`
  provides `gtimeout`. This is the outer wall-clock bound: Compose's
  `--wait-timeout` caps only the health-wait phase, so a hung `docker build` or a
  stuck image pull would otherwise be unbounded.
- The work under test must be **committed**. The build context is `git archive HEAD`,
  not the working tree (see below), so uncommitted changes are not exercised.

## What it asserts

1. `GLASSOPS_SMTP_ALLOWED_HOSTS` is actually in effect — a POST naming a
   non-allowlisted host must be refused before anything is sent. Both the status
   (exactly `400`) and the exact `detail` string are asserted: a 500 whose body
   merely mentions the variable would otherwise read as a pass. If this check does
   not fire, the rest of the run would be meaningless.
2. Saving the sink config returns 200.
3. `POST /api/alerts/test` returns 200.
4. The sink received a message whose **Subject** matches (selected by subject, not
   `messages[0]`, so a retry or stray alert cannot shift the assertion onto the wrong
   message), and whose:
   - `From` header is `alerts@example.com`
   - **`ReturnPath`** — the SMTP envelope sender, i.e. `MAIL FROM` — is the same
     address. This is the sharpest check that the null-reverse-path bug
     (`MAIL FROM:<>`, produced by an empty From) stays fixed; the header alone would
     not catch it.
   - `To` is `ops@example.com`
   - body contains the test text

   These are explicit `if … raise SystemExit(1)` checks, **not** `assert` statements:
   `python -O` and `PYTHONOPTIMIZE=1` strip asserts outright, which would let a
   completely wrong message pass every one of them.

## What it does *not* prove

- **The metric-triggered path.** The script calls `/api/alerts/test`; it never
  exercises `check_and_alert()`. The aggregate-CPU threshold logic is covered by
  `tests/backend/test_alert_service.py`.
- **The frontend.** The per-core alert regression and the Email settings tab are
  covered by the Vitest suites.
- **Delivery to a real mailbox.** Mailpit accepts and stores; it never relays. A real
  provider can still bounce or filter a message it accepted — see the README's
  "accepted ≠ delivered" note.

## Isolation

Every property below is load-bearing; changing one can turn this from a safe check
into something that damages a real deployment.

- **Unique Compose project** (`glassops-smtp-check-$$-<epoch>`). A fixed name could
  collide with an existing project, and cleanup would then tear down resources this
  script did not create.
- **Same `-f`/`-p` pair for teardown.** `docker compose down` without `-f` reads the
  repository's own compose files and `.env`, which could stop the developer's real
  stack. Cleanup uses the generated file and the unique project name, plus
  `--rmi local` to drop the image this run built.
- **Trap-based cleanup.** `trap cleanup EXIT` plus `trap 'exit 129|130|143' HUP|INT|TERM`.
  The signal traps exist so an interrupted run cannot exit 0 — a bare
  `trap cleanup TERM` would let cleanup's own success set the status.
- **PASS is printed only after a successful teardown.** Cleanup runs on EXIT, i.e.
  *after* the last line of the script, so printing PASS at the end would report a
  clean run even when containers, networks or volumes were left behind. The check
  instead tears down explicitly, and a failed teardown turns a passing run into
  exit 1 — keeping the generated compose file and printing the exact
  `docker compose -f … -p … down` command needed to finish the job by hand.
- **Every Docker call is bounded.** `docker info`, `compose down` and the build all
  run under `timeout --kill-after`, so a child that ignores SIGTERM is still killed.
  The sink polling loop uses a fractional monotonic deadline rather than an iteration
  count — 20 iterations of a 20-second curl would be ~7 minutes — and caps the inner
  request, the sleep, **and the outer `docker exec`** by the time actually left. That
  last one matters: the shared wrapper's fixed 60s (+10s kill grace) would restart on
  every iteration, so an exec that stalls before curl even runs could overrun by ~70s.
  Measured with a fully stalled exec: a 5-second budget finished in 5.09s, versus 60s
  under the fixed wrapper.
- **Transport failure is never read as an HTTP result.** The exec's exit status and
  the HTTP status are kept separate. curl can print a body and `200` and still exit
  non-zero (a mid-stream timeout), and a process killed before the status trailer
  leaves the body's last line in its place — for `/api/auth/login` that is the JSON
  holding the access and refresh tokens, which once reached an error message. Any
  transport failure, or a trailer that is not a 3-digit status, yields the fixed
  sentinel `EXEC_FAILED` and an empty body, and the login failure message is fixed
  text with nothing interpolated.
- **Cleanup state is `idle → running → done`.** Only `done` short-circuits re-entry.
  A teardown interrupted by a signal is therefore re-entered by the EXIT trap and
  still prints what was left behind, and `cleanup` returns the status it was entered
  with, so a cleanup failure after a signal keeps 143 instead of being rewritten to 1.
- **Its own database and volume.** `GLASSOPS_DB_PATH=/app/data/check.db` on a
  disposable named volume. The developer's `data/` directory, `.env`, and
  `alert_config` row are never read or written, so a real SMTP configuration cannot
  be overwritten or cleared.
- **`internal: true` network, and nothing published to the host.** The internal
  network removes the gateway, so nothing in it has egress and the sink cannot relay
  onward. Note this is **container runtime** egress only — the image pull and any
  `apt`/`npm` inside the Dockerfile still use the host's network during build.

  Port publishing and `internal: true` are mutually exclusive: Docker maps no host
  port on an internal network, and `docker compose port` then answers `invalid IP:0`
  (which a naive `sed 's/.*://'` turns into the string `0`, i.e. a request to port 0).
  The generated compose file therefore declares **no** `ports:` at all.
  Attaching a second, non-internal network would restore the host port but also
  restore egress — verified: the container could reach the public internet again.
  Every HTTP call is therefore made **inside** the network with
  `docker compose exec … curl`, so isolation is kept rather than traded away for
  reachability. The app image already ships curl for its own healthcheck.
- **Build context is `git archive HEAD`,** not `${REPO_ROOT}`. The repo has no
  `.dockerignore`, so a directory context would ship `.env`, `data/secret.key`, `.git`
  and ~200 MB of `node_modules` into the build daemon — a real secret-exposure risk,
  worse against a remote Docker context.
- **Pinned image digest.** `axllent/mailpit:v1.21.8@sha256:8137…` (an OCI multi-arch
  index including `linux/arm64`). `:latest` would make the check non-reproducible.
  Re-pin with
  `docker buildx imagetools inspect axllent/mailpit:v1.21.8 --format '{{.Manifest.Digest}}'`.

## Testing against a real provider

Out of scope for this script and **not to be run without an explicit, in-the-moment
request from the operator**, using a throwaway account and mailbox they supply. If it
is ever done:

- Credentials go in the UI or the environment — never into the repo, a compose file,
  shell history, a command line, or any log or transcript.
- An HTTP 200 from `/api/alerts/test` means the relay accepted the message. It is not
  delivery evidence.
- Final success is the actual inbox, or the provider's delivery log, showing the
  expected From, To, Subject and body.
