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
    monkeypatch.setattr(client_ws, "CLIENT_CLOSE_TIMEOUT", 0.2)
    monkeypatch.setattr(client_ws, "CLIENT_SEND_TIMEOUT", 0.05)
    sockets = [FakeSocket(close_hangs=True) for _ in range(5)]
    tasks = [asyncio.create_task(fake_handler(ws)) for ws in sockets]
    await asyncio.sleep(0)

    start = asyncio.get_event_loop().time()
    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 1}}), timeout=2
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.6  # well under 5 * CLIENT_CLOSE_TIMEOUT (1.0s) if serialized

    for ws in sockets:
        assert ws not in client_ws._clients
        assert ws.close_calls == 1
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    for ws in sockets:
        assert ws not in client_ws._client_tasks
