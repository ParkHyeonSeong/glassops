import { afterEach, describe, expect, it, vi } from "vitest";
import { deferred } from "../../test/fixtures";
import {
  _resetServerClockForTest,
  ensureServerClockSync,
  serverNowSeconds,
  subscribeServerClock,
  syncServerClock,
} from "../serverClock";

describe("serverClock", () => {
  afterEach(() => {
    _resetServerClockForTest();
    vi.unstubAllGlobals();
  });

  it("applies an rtt/2-compensated server offset", async () => {
    vi.spyOn(Date, "now")
      .mockReturnValueOnce(1_000_000)
      .mockReturnValue(1_000_200);
    vi.stubGlobal("fetch", vi.fn(async () => (
      new Response(JSON.stringify({ timestamp: 1_240 }))
    )));

    await syncServerClock();

    expect(serverNowSeconds()).toBeCloseTo(1_240.1, 5);
  });

  it("ignores an invalid payload and keeps the local clock", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    vi.stubGlobal("fetch", vi.fn(async () => (
      new Response(JSON.stringify({ utc: "not-a-number" }))
    )));

    await syncServerClock();

    expect(serverNowSeconds()).toBe(1_000);
  });

  it("keeps the last successful offset when a re-sync fails", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ timestamp: 1_100 })))
      .mockRejectedValueOnce(new Error("offline")));

    await syncServerClock();
    expect(serverNowSeconds()).toBe(1_100);

    await syncServerClock();
    expect(serverNowSeconds()).toBe(1_100);
  });

  it("discards a stale sync that resolves after a newer one", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    const slow = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn()
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(new Response(JSON.stringify({ timestamp: 2_000 }))));

    const slowSync = syncServerClock();
    await syncServerClock();
    expect(serverNowSeconds()).toBe(2_000);

    slow.resolve(new Response(JSON.stringify({ timestamp: 1_500 })));
    await slowSync;

    expect(serverNowSeconds()).toBe(2_000);
  });

  it("notifies subscribers when the offset updates", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    vi.stubGlobal("fetch", vi.fn(async () => (
      new Response(JSON.stringify({ timestamp: 1_100 }))
    )));
    const listener = vi.fn();
    const unsubscribe = subscribeServerClock(listener);

    await syncServerClock();
    expect(listener).toHaveBeenCalledOnce();

    unsubscribe();
    await syncServerClock();
    expect(listener).toHaveBeenCalledOnce();
  });

  it("skips the interval sync while a previous one is in flight", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      vi.spyOn(Date, "now").mockReturnValue(1_000_000);
      const slow = deferred<Response>();
      const fetchMock = vi.fn()
        .mockReturnValueOnce(slow.promise)
        .mockResolvedValue(new Response(JSON.stringify({ timestamp: 2_000 })));
      vi.stubGlobal("fetch", fetchMock);

      ensureServerClockSync();
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // 최초 sync가 60초를 넘겨도 interval이 새 요청을 쏘지 않는다 —
      // 새 요청이 세대를 올리면 느린 응답이 영원히 폐기되는 악순환이 생긴다.
      await vi.advanceTimersByTimeAsync(60_000);
      expect(fetchMock).toHaveBeenCalledTimes(1);

      slow.resolve(new Response(JSON.stringify({ timestamp: 1_500 })));
      await vi.advanceTimersByTimeAsync(60_000);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("isolates a throwing listener from the others", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    vi.stubGlobal("fetch", vi.fn(async () => (
      new Response(JSON.stringify({ timestamp: 1_100 }))
    )));
    const broken = vi.fn(() => {
      throw new Error("subscriber bug");
    });
    const healthy = vi.fn();
    subscribeServerClock(broken);
    subscribeServerClock(healthy);

    await syncServerClock();

    expect(broken).toHaveBeenCalledOnce();
    expect(healthy).toHaveBeenCalledOnce();
    expect(serverNowSeconds()).toBe(1_100);
  });

  it("rejects a non-finite timestamp", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    // JSON.parse("1e309") === Infinity — stringify로는 만들 수 없어 raw body 사용.
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"timestamp":1e309}')));

    await syncServerClock();

    expect(serverNowSeconds()).toBe(1_000);
  });
});
