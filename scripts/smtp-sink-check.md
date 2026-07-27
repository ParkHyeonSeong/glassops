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
suites must not be reported as covering it. The causes are: no `docker` CLI, no
reachable Docker daemon, no `git`, no `timeout`/`gtimeout`, or an unpinned Mailpit
digest.

### Prerequisites

- A **running** Docker daemon (`docker info` must succeed — installing Docker is not
  enough).
- `timeout` or `gtimeout` on `PATH`. macOS ships neither; `brew install coreutils`
  provides `gtimeout`. This is the outer wall-clock bound: Compose's
  `--wait-timeout` caps only the health-wait phase, so a hung `docker build` or a
  stuck image pull would otherwise be unbounded.
- The work under test must be **committed**. The build context is `git archive HEAD`,
  not the working tree (see below), so uncommitted changes are not exercised.

## What it asserts

1. `GLASSOPS_SMTP_ALLOWED_HOSTS` is actually in effect — a POST naming a
   non-allowlisted host must be refused before anything is sent. If this check does
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
- **Its own database and volume.** `GLASSOPS_DB_PATH=/app/data/check.db` on a
  disposable named volume. The developer's `data/` directory, `.env`, and
  `alert_config` row are never read or written, so a real SMTP configuration cannot
  be overwritten or cleared.
- **`internal: true` network.** This removes the gateway, so nothing in the network
  has egress and the sink cannot relay onward. Note this is **container runtime**
  egress only — the image pull and any `apt`/`npm` inside the Dockerfile still use the
  host's network during build.
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
