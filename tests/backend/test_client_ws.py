"""Client WebSocket eviction — a timed-out client must be both de-registered
AND closed, so its handler task (parked in `await ws.receive_text()`) is
unblocked instead of leaking an accepted socket + ASGI task forever.

FakeSocket below models just enough of the real lifecycle to prove this:
receive_text() parks on an Event that only close() can set (mirroring how a
real ASGI close() unblocks a pending receive), and a small handler coroutine
mirrors handle_client_ws's own register/finally-deregister shape so these
tests exercise the SAME _clients/_client_tasks bookkeeping broadcast_to_clients
must cooperate with.
"""

import asyncio

import pytest
from fastapi import WebSocketDisconnect

import app.websocket.client_ws as client_ws


class FakeSocket:
    """A fake WS: send_text always stalls (forcing eviction); close either
    unblocks the parked receive_text (normal case) or hangs forever (the
    close-hangs case), in which case only cancellation can end the handler."""

    def __init__(self, *, close_hangs: bool = False):
        self._closed_event = asyncio.Event()
        self.close_hangs = close_hangs
        self.close_calls = 0
        self.send_calls = 0
        self.accept_calls = 0

    async def accept(self, subprotocol: str | None = None) -> None:
        # Only exercised by tests that drive the real handle_client_ws, which
        # awaits this during its auth handshake before registering the client.
        self.accept_calls += 1

    async def send_text(self, payload: str) -> None:
        self.send_calls += 1
        await asyncio.sleep(3600)  # always stalls -> triggers eviction

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_hangs:
            await asyncio.sleep(3600)
        self._closed_event.set()

    async def receive_text(self) -> str:
        await self._closed_event.wait()
        raise WebSocketDisconnect()


async def fake_handler(ws: FakeSocket) -> None:
    """Mirrors handle_client_ws's registration and finally-deregistration,
    without the auth handshake — the eviction path only depends on this
    shape, not on how the client got here."""
    client_ws._clients.add(ws)
    client_ws._client_tasks[ws] = asyncio.current_task()
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        client_ws._clients.discard(ws)
        client_ws._client_tasks.pop(ws, None)


async def _fake_get_user(email: str) -> dict:
    """Stand-in for app.database.get_user: an active user in good standing, so
    handle_client_ws's is_active/must_change_password gates both pass."""
    return {"is_active": True, "must_change_password": False}


async def _fake_access_revoked(token: str, user: dict) -> bool:
    """Stand-in for auth_service.access_revoked: token not revoked."""
    return False


async def _run_real_handler(monkeypatch, ws: "FakeSocket") -> asyncio.Task:
    """Drives the REAL handle_client_ws (not fake_handler) up through its auth
    handshake — origin check, token extraction, token verification, user lookup,
    must-change-password gate, revocation gate, and ws.accept — by stubbing each
    dependency it calls, then returns once it has registered and is parked in
    receive_text(). This is what proves fake_handler's registration shape (used
    by every other test in this file) actually matches production, instead of
    just asserting against itself."""
    monkeypatch.setattr(client_ws, "origin_ok", lambda ws: True)
    monkeypatch.setattr(client_ws, "ws_token", lambda ws: "tok")
    monkeypatch.setattr(client_ws, "verify_token", lambda tok: "user@example.com")
    monkeypatch.setattr(client_ws, "get_user", _fake_get_user)
    monkeypatch.setattr(client_ws, "access_revoked", _fake_access_revoked)
    monkeypatch.setattr(client_ws, "accept_subprotocol", lambda ws: None)
    task = asyncio.create_task(client_ws.handle_client_ws(ws))
    await asyncio.sleep(0)  # let the handshake run to completion and the handler register
    return task


@pytest.fixture(autouse=True)
def _clean_client_registry():
    # Isolate each test's registry — a leaked entry from a prior test must
    # not change which clients broadcast_to_clients snapshots.
    client_ws._clients.clear()
    client_ws._client_tasks.clear()
    yield
    client_ws._clients.clear()
    client_ws._client_tasks.clear()


async def _run_eviction(monkeypatch, ws: FakeSocket) -> asyncio.Task:
    monkeypatch.setattr(client_ws, "CLIENT_SEND_TIMEOUT", 0.05)
    monkeypatch.setattr(client_ws, "CLIENT_CLOSE_TIMEOUT", 0.05)
    task = asyncio.create_task(fake_handler(ws))
    await asyncio.sleep(0)  # let the handler register before we broadcast

    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 1}}), timeout=1
    )
    return task


async def test_timed_out_client_is_closed_and_handler_finishes(monkeypatch):
    ws = FakeSocket()
    task = await _run_eviction(monkeypatch, ws)

    assert ws not in client_ws._clients               # de-registered
    assert ws.close_calls == 1                          # close() WAS called

    # close() unblocked receive_text -> the handler task runs to completion
    # (not left cancelled) on its own, via WebSocketDisconnect + finally.
    await asyncio.wait_for(task, timeout=1)
    assert task.done()
    assert ws not in client_ws._client_tasks             # no pending task left behind


async def test_close_hangs_falls_back_to_cancelling_the_handler(monkeypatch):
    ws = FakeSocket(close_hangs=True)
    task = await _run_eviction(monkeypatch, ws)

    assert ws not in client_ws._clients                 # de-registered regardless
    assert ws.close_calls == 1                          # close() WAS attempted

    # close() never returns, so the fallback must cancel the handler task —
    # it must not stay parked in receive_text() forever.
    results = await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True), timeout=1
    )
    assert task.done()
    assert isinstance(results[0], asyncio.CancelledError)
    assert ws not in client_ws._client_tasks             # no pending task left behind


async def test_broadcast_evicts_many_stalled_clients_concurrently(monkeypatch):
    # N stalled clients must cost one CLIENT_CLOSE_TIMEOUT, not N — eviction
    # must run concurrently (asyncio.gather), not sequentially.
    monkeypatch.setattr(client_ws, "CLIENT_CLOSE_TIMEOUT", 1.0)
    monkeypatch.setattr(client_ws, "CLIENT_SEND_TIMEOUT", 0.05)
    sockets = [FakeSocket(close_hangs=True) for _ in range(5)]
    tasks = [asyncio.create_task(fake_handler(ws)) for ws in sockets]
    await asyncio.sleep(0)

    start = asyncio.get_event_loop().time()
    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 1}}), timeout=5
    )
    elapsed = asyncio.get_event_loop().time() - start
    # ~1.05s expected (concurrent). Bound has wide margin on both sides: well
    # above expected (absorbs CI scheduling jitter without flaking) and well
    # below 5 * CLIENT_CLOSE_TIMEOUT (5.0s), which is what a serialization
    # regression would cost — so this still catches that regression.
    assert elapsed < 2.0

    for ws in sockets:
        assert ws not in client_ws._clients
        assert ws.close_calls == 1
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    for ws in sockets:
        assert ws not in client_ws._client_tasks


async def test_cancelling_evict_client_still_cancels_the_handler(monkeypatch):
    # _evict_client itself can be cancelled mid-eviction (e.g. its caller's
    # caller was cancelled) while awaiting one of the internal wait_for
    # calls. asyncio.CancelledError is a BaseException, not an Exception, so
    # a bare `except Exception: pass` around those awaits does not catch it
    # — if the task.cancel() fallback lived after that except block instead
    # of in a finally, cancellation would skip it entirely. Since the client
    # was already de-registered from both _clients and _client_tasks before
    # the awaits even started, nothing else could ever find and cancel that
    # handler again — it would stay parked in receive_text() forever.
    monkeypatch.setattr(client_ws, "CLIENT_CLOSE_TIMEOUT", 5)
    ws = FakeSocket(close_hangs=True)
    handler_task = asyncio.create_task(fake_handler(ws))
    await asyncio.sleep(0)  # let the handler register

    evict_task = asyncio.create_task(client_ws._evict_client(ws))
    await asyncio.sleep(0)  # let it de-register and start awaiting the hung close()
    evict_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(evict_task, timeout=1)

    assert ws not in client_ws._clients               # de-registered before the hang
    assert ws not in client_ws._client_tasks

    # The fallback must still cancel the handler despite _evict_client's own
    # cancellation — bounded wait so a regression fails fast instead of
    # hanging the suite.
    results = await asyncio.wait_for(
        asyncio.gather(handler_task, return_exceptions=True), timeout=1
    )
    assert handler_task.done()
    assert isinstance(results[0], asyncio.CancelledError)


async def test_real_handler_registers_both_registries_and_cleans_up_on_disconnect(monkeypatch):
    # Everything above drives fake_handler, which only mirrors handle_client_ws's
    # registration shape — it does not prove production actually does this. This
    # test runs the REAL handle_client_ws through a stubbed auth handshake instead.
    ws = FakeSocket()
    handler_task = await _run_real_handler(monkeypatch, ws)

    assert ws.accept_calls == 1                          # handshake reached ws.accept()
    assert ws in client_ws._clients                       # real handler registered itself...
    assert client_ws._client_tasks[ws] is handler_task    # ...under its OWN running task

    await ws.close()  # a normal client-initiated close -> receive_text raises WebSocketDisconnect
    await asyncio.wait_for(handler_task, timeout=1)
    assert handler_task.done()

    # The real handler's `finally` must drain BOTH registries, same as fake_handler's.
    assert ws not in client_ws._clients
    assert ws not in client_ws._client_tasks


async def test_real_handler_terminates_on_send_timeout_eviction(monkeypatch):
    # Same as test_timed_out_client_is_closed_and_handler_finishes above, but
    # against the REAL handle_client_ws instead of fake_handler.
    monkeypatch.setattr(client_ws, "CLIENT_SEND_TIMEOUT", 0.05)
    monkeypatch.setattr(client_ws, "CLIENT_CLOSE_TIMEOUT", 0.05)
    ws = FakeSocket()
    handler_task = await _run_real_handler(monkeypatch, ws)

    assert ws in client_ws._clients
    assert client_ws._client_tasks[ws] is handler_task

    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 1}}), timeout=1
    )

    # send_text always stalls -> _evict_client closes ws -> receive_text raises
    # WebSocketDisconnect -> the real handler task must actually terminate, not
    # stay parked forever.
    await asyncio.wait_for(handler_task, timeout=1)
    assert handler_task.done()
    assert ws not in client_ws._clients
    assert ws not in client_ws._client_tasks
