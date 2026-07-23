import asyncio
import base64
import json
import logging
import threading
import time
from email.message import Message

import aiosmtplib
import pytest
from cryptography.fernet import Fernet

import app.database as db
from app.services import alert_service as svc


def _reset_alert_state():
    """Reset module state using only primitives that exist BEFORE the implementation
    lands. Referencing svc._reset_alert_state_for_test() directly would raise
    AttributeError on the RED run, before the fixture's yield — teardown would be
    skipped and the non-daemon aiosqlite thread would hold pytest open."""
    reset = getattr(svc, "_reset_alert_state_for_test", None)
    if reset is not None:
        reset()
        return
    svc._last_sent.clear()
    getattr(svc, "_last_attempt", {}).clear()
    svc._invalidate_config_cache()


@pytest.fixture
async def store(tmp_path, monkeypatch):
    """Isolated DB + clean module-level cooldown/cache state."""
    await db.close_db()          # never inherit another module's connection
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "alerts.db"))
    monkeypatch.setattr(db, "_conn", None)
    try:
        await db.init_db()
        _reset_alert_state()
        yield
    finally:
        _reset_alert_state()
        await db.close_db()


class FakeSend:
    """Records every aiosmtplib.send() call; optionally raises."""

    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.messages: list[Message] = []
        self.error = error

    async def __call__(self, message, **kwargs):
        self.messages.append(message)
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ({}, "250 OK")


def base_config(**overrides) -> dict:
    cfg = {
        "host": "relay.example.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "alerts@example.com",
        "to_email": "ops@example.com",
        "security": "starttls",
        "use_tls": False,
        "start_tls": True,
        "thresholds": {"cpu_crit": 90, "mem_crit": 90, "disk_crit": 95},
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def allow_target(monkeypatch):
    monkeypatch.setattr(svc, "validate_smtp_target", lambda host, port: None)


@pytest.fixture
def fake_send(monkeypatch):
    sender = FakeSend()
    monkeypatch.setattr(svc.aiosmtplib, "send", sender)
    return sender


async def raw_stored_config() -> dict:
    conn = await db.get_db()
    cursor = await conn.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    return json.loads(row["config"]) if row else {}


def decrypt_stored(raw: dict) -> str:
    """Decrypt what is actually on disk. Fernet embeds a random IV, so re-encrypting
    the SAME password yields a DIFFERENT token — comparing ciphertexts can only show
    that re-encryption happened, never that the password changed."""
    return svc._decrypt(raw["password_enc"])


# --- storage / encryption ------------------------------------------------

async def test_password_round_trips_through_encryption(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))

    assert (await svc.get_smtp_config())["password"] == "pw-under-test"


async def test_stored_row_holds_no_plaintext_password(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))

    raw = await raw_stored_config()

    assert "password" not in raw
    assert "pw-under-test" not in json.dumps(raw)
    assert raw["password_enc"]


async def test_mask_sentinel_preserves_the_existing_ciphertext(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))
    before = (await raw_stored_config())["password_enc"]

    await svc.save_smtp_config(base_config(password=svc.MASKED_PASSWORD))

    assert (await raw_stored_config())["password_enc"] == before
    assert (await svc.get_smtp_config())["password"] == "pw-under-test"


async def test_empty_password_preserves_the_existing_ciphertext(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))
    before = (await raw_stored_config())["password_enc"]

    await svc.save_smtp_config(base_config(password=""))

    assert (await raw_stored_config())["password_enc"] == before


async def test_new_password_replaces_the_ciphertext(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))

    await svc.save_smtp_config(base_config(password="pw-rotated"))

    # Decrypt, don't compare ciphertexts: Fernet is non-deterministic, so an
    # inequality assertion would also pass when the password never changed.
    assert decrypt_stored(await raw_stored_config()) == "pw-rotated"
    assert (await svc.get_smtp_config())["password"] == "pw-rotated"


async def test_clear_password_removes_the_stored_credential(store):
    await svc.save_smtp_config(base_config(password="pw-under-test"))

    await svc.save_smtp_config(base_config(password="", clear_password=True))

    raw = await raw_stored_config()
    assert "password_enc" not in raw
    assert "clear_password" not in raw
    # get_smtp_config only writes "password" when a password_enc exists, so the
    # key is legitimately absent after a clear.
    assert (await svc.get_smtp_config()).get("password", "") == ""


async def test_config_cache_returns_an_isolated_copy(store):
    await svc.save_smtp_config(base_config())

    first = await svc.get_smtp_config()
    first["host"] = "mutated.example.com"
    first["thresholds"]["cpu_crit"] = 1

    second = await svc.get_smtp_config()
    assert second["host"] == "relay.example.com"
    assert second["thresholds"]["cpu_crit"] == 90


async def test_save_invalidates_the_config_cache(store):
    await svc.save_smtp_config(base_config())
    await svc.get_smtp_config()  # populate the cache

    await svc.save_smtp_config(base_config(to_email="oncall@example.com"))

    assert (await svc.get_smtp_config())["to_email"] == "oncall@example.com"


async def _write_raw_config(raw: dict) -> None:
    """Write the stored row verbatim, bypassing save_smtp_config's encryption."""
    conn = await db.get_db()
    await conn.execute(
        "INSERT OR REPLACE INTO alert_config (id, config) VALUES (1, ?)", (json.dumps(raw),))
    await conn.commit()
    svc._invalidate_config_cache()


# A token this process's key cannot open — exactly what a rotated
# GLASSOPS_SECRET_KEY leaves behind on disk.
FOREIGN_TOKEN = Fernet(base64.urlsafe_b64encode(b"\xAA" * 32)).encrypt(b"pw-under-test").decode()


@pytest.mark.parametrize("password_enc", [
    FOREIGN_TOKEN,        # right shape, wrong key (secret rotation)
    "not-a-fernet-token",  # corrupted ciphertext
    "",                    # empty
    12345,                 # non-string — must fail closed, not raise
])
async def test_undecryptable_password_is_surfaced_and_blocks_sending(
    store, allow_target, fake_send, password_enc,
):
    """Exercises the REAL Fernet boundary. Monkeypatching _decrypt would let its
    InvalidToken handling be deleted outright with every test still green."""
    raw = base_config()
    raw.pop("password", None)
    raw["password_enc"] = password_enc
    await _write_raw_config(raw)

    config = await svc.get_smtp_config()

    assert config.get("_decrypt_failed") is True
    assert config.get("password", "") == ""
    # The internal ciphertext must never reach a caller (the API strips only keys
    # starting with "_", so a surviving password_enc would be served to the client).
    assert "password_enc" not in config

    result = await svc.send_alert_email("s", "b")
    assert result["ok"] is False
    assert "decrypt" in result["error"].lower()
    assert fake_send.calls == []


# --- TLS mode safety -----------------------------------------------------

@pytest.mark.parametrize("stored", [
    {"security": "ssl", "use_tls": False, "start_tls": False},   # would become PLAINTEXT
    {"security": "ssl", "use_tls": True, "start_tls": False},
    {"security": "SSL", "use_tls": False, "start_tls": True},    # wrong case
    {"security": "tls", "use_tls": False, "start_tls": True},
    {"security": 123, "use_tls": False, "start_tls": False},
])
def test_unknown_security_mode_is_refused_not_downgraded(stored):
    """An explicit but unsupported mode must NOT fall back to the legacy flags —
    'ssl' with both flags false would silently resolve to plaintext SMTP."""
    assert svc.security_mode(stored) is None


@pytest.mark.parametrize("stored", [
    {"use_tls": "false", "start_tls": "false"},   # bool("false") is True
    {"use_tls": 1, "start_tls": 0},
    {"use_tls": None, "start_tls": None},
])
def test_malformed_legacy_tls_flags_are_refused(stored):
    assert svc.security_mode(stored) is None


def test_absent_security_still_derives_from_legacy_flags():
    assert svc.security_mode({"use_tls": True, "start_tls": False}) == "implicit_tls"
    assert svc.security_mode({"use_tls": False, "start_tls": True}) == "starttls"
    assert svc.security_mode({"use_tls": False, "start_tls": False}) == "none"
    assert svc.security_mode({}) == "starttls"          # start_tls defaults true
    assert svc.security_mode({"security": None, "use_tls": True, "start_tls": False}) \
        == "implicit_tls"                                # explicit null == absent


async def test_unknown_stored_security_mode_blocks_sending(store, allow_target, fake_send):
    raw = base_config(security="ssl", use_tls=False, start_tls=False)
    raw.pop("password", None)
    await _write_raw_config(raw)

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert "ambiguous" in result["error"].lower()
    assert fake_send.calls == []


# --- credential completeness ---------------------------------------------

async def test_username_without_password_is_refused(store, allow_target, fake_send):
    """aiosmtplib authenticates whenever username is not None and turns a None
    password into "", so a username left behind after clear_password would retry
    AUTH PLAIN with a blank secret — repeated failures or an account lockout."""
    await svc.save_smtp_config(base_config(username="relay-login", password=""))

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert "credential" in result["error"].lower()
    assert fake_send.calls == []


async def test_password_without_username_is_refused(store, allow_target, fake_send):
    # The reverse is silently ignored by aiosmtplib — the operator thinks the relay
    # is authenticated when it is not.
    await svc.save_smtp_config(base_config(username="", password="pw-under-test"))

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert "credential" in result["error"].lower()
    assert fake_send.calls == []


# --- clock ---------------------------------------------------------------

def test_duration_policies_use_a_monotonic_clock(monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 10_000)
    first = svc._now()
    monkeypatch.setattr(time, "time", lambda: real() - 10_000)
    second = svc._now()

    # A wall-clock step in either direction must not move this clock.
    assert second >= first
    assert abs(second - first) < 5


async def test_cooldown_is_not_expired_early_by_a_forward_clock_step(
    store, allow_target, fake_send, monkeypatch,
):
    await svc.save_smtp_config(base_config())
    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is True

    # NTP or a VM resume steps the wall clock an hour forward.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)

    assert (await svc.send_alert_email("s", "b", key="alert-a1")) == {
        "ok": False, "error": "Cooldown active"}
    assert len(fake_send.calls) == 1


async def test_backoff_is_not_extended_by_a_backward_clock_step(
    store, allow_target, monkeypatch,
):
    await svc.save_smtp_config(base_config())
    monkeypatch.setattr(svc.aiosmtplib, "send", FakeSend(error=OSError("refused")))
    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is False

    # Clock corrected an hour backwards: with a wall clock this would suppress the
    # alert for an extra hour.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3600)

    succeeding = FakeSend()
    monkeypatch.setattr(svc.aiosmtplib, "send", succeeding)
    svc._last_attempt["alert-a1"] -= svc.FAILURE_BACKOFF_SECONDS + 1

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is True
    assert len(succeeding.calls) == 1


# --- transport arguments -------------------------------------------------

async def test_blank_credentials_are_passed_as_none(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config(username="", password=""))

    assert (await svc.send_alert_email("s", "b"))["ok"] is True

    call = fake_send.calls[0]
    assert call["username"] is None
    assert call["password"] is None


async def test_real_credentials_are_passed_through(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config(username="relay-login", password="pw-under-test"))

    await svc.send_alert_email("s", "b")

    call = fake_send.calls[0]
    assert call["username"] == "relay-login"
    assert call["password"] == "pw-under-test"


@pytest.mark.parametrize("mode,use_tls,start_tls", [
    ("starttls", False, True),
    ("implicit_tls", True, False),
    ("none", False, False),
])
async def test_security_mode_maps_to_exclusive_tls_flags(
    store, allow_target, fake_send, mode, use_tls, start_tls,
):
    await svc.save_smtp_config(base_config(security=mode))

    await svc.send_alert_email("s", "b")

    call = fake_send.calls[0]
    assert call["use_tls"] is use_tls
    assert call["start_tls"] is start_tls
    # aiosmtplib raises ValueError if both are set.
    assert not (call["use_tls"] and call["start_tls"])


async def test_legacy_config_without_security_is_back_filled(store, allow_target, fake_send):
    # Rows written before `security` existed carry only the two flags.
    legacy = base_config(use_tls=True, start_tls=False)
    del legacy["security"]
    await svc.save_smtp_config(legacy)

    await svc.send_alert_email("s", "b")

    call = fake_send.calls[0]
    assert (call["use_tls"], call["start_tls"]) == (True, False)


async def test_ambiguous_legacy_tls_flags_block_sending(store, allow_target, fake_send):
    # Both flags true is a state aiosmtplib refuses outright. Guessing a mode would
    # silently up- or downgrade the operator's transport.
    legacy = base_config(use_tls=True, start_tls=True)
    del legacy["security"]
    await svc.save_smtp_config(legacy)

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert "ambiguous" in result["error"].lower()
    assert fake_send.calls == []


async def test_message_headers_and_body(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.send_alert_email("[GlassOps] Test Alert", "line one\nline two")

    msg = fake_send.messages[0]
    assert msg["From"] == "alerts@example.com"
    assert msg["To"] == "ops@example.com"
    assert msg["Subject"] == "[GlassOps] Test Alert"
    assert msg.get_payload(decode=True).decode() == "line one\nline two"
    assert fake_send.calls[0]["hostname"] == "relay.example.com"
    assert fake_send.calls[0]["port"] == 587


# --- sender resolution ---------------------------------------------------

async def test_sender_falls_back_to_an_email_username(store, allow_target, fake_send):
    # A username is only ever present on an authenticated relay, so the fallback is
    # exercised with a password set — username-without-password is refused outright
    # (see test_username_without_password_is_refused).
    await svc.save_smtp_config(
        base_config(from_email="", username="relay@example.com", password="pw-under-test"))

    await svc.send_alert_email("s", "b")

    assert fake_send.messages[0]["From"] == "relay@example.com"


async def test_non_email_username_is_not_used_as_sender(store, allow_target, fake_send):
    # A login identifier is not an address — sending MAIL FROM:<relay-login> or
    # MAIL FROM:<> would be silently undeliverable.
    await svc.save_smtp_config(base_config(from_email="", username="relay-login"))

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert "From" in result["error"]
    assert fake_send.calls == []


async def test_sender_never_resolves_to_an_empty_address(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config(from_email="", username=""))

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert fake_send.calls == []


async def test_stored_invalid_from_email_is_refused_at_send_time(store, allow_target, fake_send):
    # Rows written before the API validated from_email are still on disk; this is
    # the last gate before the value reaches the wire.
    await svc.save_smtp_config(base_config(from_email="not-an-address"))

    result = await svc.send_alert_email("s", "b")

    assert result["ok"] is False
    assert fake_send.calls == []


# --- cooldown ------------------------------------------------------------

async def test_failed_send_backs_off_briefly_but_not_for_the_full_cooldown(
    store, allow_target, monkeypatch,
):
    await svc.save_smtp_config(base_config())
    monkeypatch.setattr(svc.aiosmtplib, "send", FakeSend(error=OSError("connection refused")))

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is False
    # A failure must still throttle: check_and_alert is awaited inline in the ingest
    # loop and agents collect once per second, so an unthrottled failure path would
    # mean one blocking SMTP attempt per second per agent.
    assert (await svc.send_alert_email("s", "b", key="alert-a1")) == {
        "ok": False, "error": "Backing off after a failed send"}

    succeeding = FakeSend()
    monkeypatch.setattr(svc.aiosmtplib, "send", succeeding)
    # Age the attempt past the short backoff — far short of COOLDOWN_SECONDS.
    svc._last_attempt["alert-a1"] -= svc.FAILURE_BACKOFF_SECONDS + 1

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is True
    assert len(succeeding.calls) == 1
    assert svc.FAILURE_BACKOFF_SECONDS < svc.COOLDOWN_SECONDS


async def test_unconfigured_failure_does_not_burn_the_cooldown(store, allow_target, fake_send):
    # No config row at all — the old code recorded the cooldown before this check.
    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is False

    await svc.save_smtp_config(base_config())
    svc._invalidate_config_cache()

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is True


async def test_successful_send_starts_the_cooldown(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is True
    second = await svc.send_alert_email("s", "b", key="alert-a1")

    assert second == {"ok": False, "error": "Cooldown active"}
    assert len(fake_send.calls) == 1


async def test_cooldown_is_scoped_per_agent(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.send_alert_email("s", "b", key="alert-a1")
    result = await svc.send_alert_email("s", "b", key="alert-a2")

    assert result["ok"] is True
    assert len(fake_send.calls) == 2


async def test_manual_test_send_bypasses_the_backoff(store, allow_target, monkeypatch):
    """POST /api/alerts/test passes no key, so an admin can retry immediately even
    while automatic delivery for that agent is backing off."""
    await svc.save_smtp_config(base_config())
    monkeypatch.setattr(svc.aiosmtplib, "send", FakeSend(error=OSError("refused")))
    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is False

    succeeding = FakeSend()
    monkeypatch.setattr(svc.aiosmtplib, "send", succeeding)

    assert (await svc.send_alert_email("manual", "b"))["ok"] is True
    assert len(succeeding.calls) == 1


# --- error text ----------------------------------------------------------

SECRETS = ("pw-under-test", "relay-login", "alerts@example.com", "ops@example.com")


@pytest.mark.parametrize("error,expected", [
    # The AUTH PLAIN blob a rejecting server echoes back decodes to the credentials.
    (aiosmtplib.SMTPAuthenticationError(
        535, "AUTH PLAIN " + base64.b64encode(b"\0relay-login\0pw-under-test").decode()),
     "SMTP authentication failed"),
    # These two carry the REAL envelope addresses in .args.
    (aiosmtplib.SMTPSenderRefused(550, "sender denied", "alerts@example.com"),
     "SMTP sender rejected"),
    (aiosmtplib.SMTPRecipientsRefused(
        [aiosmtplib.SMTPRecipientRefused(550, "no such user", "ops@example.com")]),
     "SMTP recipient rejected"),
    (aiosmtplib.SMTPTimeoutError("timed out talking to relay.example.com"),
     "SMTP connection timed out"),
    (aiosmtplib.SMTPConnectError("cannot reach relay.example.com"),
     "SMTP connection failed"),
    (RuntimeError("unexpected: pw-under-test leaked into the message"),
     "SMTP send failed"),
])
async def test_transport_errors_return_a_fixed_message_with_no_secrets(
    store, allow_target, monkeypatch, caplog, error, expected,
):
    """Nothing derived from the exception text may reach the API or the log.
    Scrubbing known substrings is a denylist and cannot be complete."""
    await svc.save_smtp_config(base_config(username="relay-login", password="pw-under-test"))
    monkeypatch.setattr(svc.aiosmtplib, "send", FakeSend(error=error))

    with caplog.at_level(logging.ERROR, logger="glassops.alerts"):
        result = await svc.send_alert_email("s", "b")

    assert result == {"ok": False, "error": expected}
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "glassops.alerts")
    for secret in SECRETS:
        assert secret not in result["error"]
        assert secret not in logged
    assert base64.b64encode(b"\0relay-login\0pw-under-test").decode() not in logged


# --- DNS boundary --------------------------------------------------------

async def test_send_time_dns_check_does_not_block_the_event_loop(store, fake_send, monkeypatch):
    """validate_smtp_target calls socket.getaddrinfo synchronously. Left on the loop
    it stalls every other request and every other agent's ingest, not just this
    coroutine — so it must run in a worker thread."""
    await svc.save_smtp_config(base_config())
    # threading.Event, not asyncio.Event: set from the worker thread, and asyncio
    # primitives are not thread-safe.
    started = threading.Event()
    release = threading.Event()

    BLOCK_CEILING = 2.0        # what a synchronous validator would cost the loop

    def slow_validate(host, port):
        started.set()
        release.wait(BLOCK_CEILING)

    monkeypatch.setattr(svc, "validate_smtp_target", slow_validate)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    # Start the send FIRST — `started` is set from inside it, so waiting on the flag
    # before calling it would deadlock.
    send_task = asyncio.create_task(svc.send_alert_email("s", "b"))
    began = time.monotonic()
    try:
        assert await asyncio.to_thread(started.wait, 5), "validator was never called"
        await asyncio.sleep(0.15)          # the ticker must advance while DNS hangs
        ticks_during_block = ticks
        release.set()
        result = await asyncio.wait_for(send_task, timeout=10)
        assert result["ok"] is True
    finally:
        release.set()
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass
    elapsed = time.monotonic() - began

    # Elapsed is the real discriminator. If the validator runs ON the loop, nothing
    # can call release.set() while it blocks, so it runs to its own BLOCK_CEILING and
    # the ticker only catches up afterwards — a ticks-only assertion passes for the
    # wrong reason. Off-loop, the test releases it after ~0.15s.
    assert elapsed < BLOCK_CEILING / 2, (
        f"event loop was blocked during DNS resolution (elapsed={elapsed:.2f}s)")
    assert ticks_during_block > 5, f"ticker starved while DNS was hanging ({ticks_during_block})"


async def test_send_time_dns_check_is_bounded_by_a_timeout(store, fake_send, monkeypatch):
    """A resolver that hangs must not hold the send (and thus the ingest loop) open.
    to_thread alone frees the loop but leaves the awaiting coroutine waiting forever."""
    await svc.save_smtp_config(base_config(username="relay-login", password="pw-under-test"))
    monkeypatch.setattr(svc, "DNS_TIMEOUT", 0.05)
    release = threading.Event()

    def hang(host, port):
        release.wait(5)          # released in finally: no orphan thread at teardown

    monkeypatch.setattr(svc, "validate_smtp_target", hang)

    try:
        result = await asyncio.wait_for(svc.send_alert_email("s", "b"), timeout=5)
    finally:
        release.set()

    assert result == {"ok": False, "error": "SMTP host not allowed"}
    assert "pw-under-test" not in result["error"]
    assert fake_send.calls == []


async def test_dns_failure_is_throttled_like_any_other_failed_attempt(
    store, fake_send, monkeypatch,
):
    """DNS is a network call that can hang. If the attempt were recorded only after
    it succeeded, a failing resolver would be re-invoked once per collection tick."""
    await svc.save_smtp_config(base_config())
    calls = 0

    def failing_validate(host, port):
        nonlocal calls
        calls += 1
        raise ValueError("SMTP host does not resolve")

    monkeypatch.setattr(svc, "validate_smtp_target", failing_validate)

    assert (await svc.send_alert_email("s", "b", key="alert-a1"))["ok"] is False
    second = await svc.send_alert_email("s", "b", key="alert-a1")

    assert second == {"ok": False, "error": "Backing off after a failed send"}
    assert calls == 1, "the resolver was called again inside the backoff window"


# --- threshold evaluation ------------------------------------------------

async def test_check_and_alert_ignores_per_core_cpu(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.check_and_alert("a1", {
        "cpu": {"percent_total": 40, "percent_per_core": [100] * 32},
        "memory": {"percent": 10},
        "disk": {"percent": 10},
    })

    assert fake_send.calls == []


async def test_check_and_alert_sends_one_combined_message(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.check_and_alert("a1", {
        "cpu": {"percent_total": 97, "percent_per_core": [97]},
        "memory": {"percent": 96},
        "disk": {"percent": 99},
    })

    assert len(fake_send.calls) == 1
    body = fake_send.messages[0].get_payload(decode=True).decode()
    assert "Agent: a1" in body
    assert "CPU critical: 97.0%" in body
    assert "Memory critical: 96.0%" in body
    assert "Disk critical: 99.0%" in body
    assert fake_send.messages[0]["Subject"] == "[GlassOps] Alert — a1"


async def test_check_and_alert_fires_at_the_exact_threshold(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.check_and_alert("a1", {
        "cpu": {"percent_total": 90.0, "percent_per_core": [90.0]},
        "memory": {"percent": 0},
        "disk": {"percent": 0},
    })

    assert len(fake_send.calls) == 1
    assert "CPU critical: 90.0%" in fake_send.messages[0].get_payload(decode=True).decode()


async def test_check_and_alert_stays_quiet_below_the_threshold(store, allow_target, fake_send):
    await svc.save_smtp_config(base_config())

    await svc.check_and_alert("a1", {
        "cpu": {"percent_total": 89.9, "percent_per_core": [89.9]},
        "memory": {"percent": 0},
        "disk": {"percent": 0},
    })

    assert fake_send.calls == []


# --- metric ingest isolation ---------------------------------------------

async def test_alert_failure_does_not_roll_back_the_stored_metric(store, monkeypatch):
    """agent_ws.ingest_metric runs check_and_alert outside the durable contract."""
    from app.websocket import agent_ws

    async def boom(agent_id, metrics):
        raise RuntimeError("SMTP relay unreachable")

    monkeypatch.setattr(agent_ws, "check_and_alert", boom)

    broadcast: list[dict] = []

    # agent_ws.py calls broadcast_to_clients(agent_id, data) — two positional args.
    # A one-arg fake would TypeError inside _persist_and_broadcast, get swallowed by
    # its except, and the test would fail on the wrong assertion.
    async def capture(agent_id, data):
        broadcast.append(data)

    monkeypatch.setattr(agent_ws, "broadcast_to_clients", capture)

    payload = {
        "cpu": {"percent_total": 99, "percent_per_core": [99]},
        "memory": {"percent": 99},
        "disk": {"percent": 99},
        "timestamp": time.time(),
    }
    await agent_ws.ingest_metric("a1", payload)

    rows = await db.get_recent_metrics("a1", limit=10)
    assert len(rows) == 1
    assert broadcast, "live broadcast must still have happened"
