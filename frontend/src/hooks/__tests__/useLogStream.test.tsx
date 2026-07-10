import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLogStream } from "../useLogStream";

vi.mock("../../stores/authStore", () => ({
  useAuthStore: {
    getState: () => ({ accessToken: null }),
  },
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  close = vi.fn();

  constructor(url: string, protocols?: string | string[]) {
    void url;
    void protocols;
    FakeWebSocket.instances.push(this);
  }

  emitLine(line: string) {
    this.onmessage?.({
      data: JSON.stringify({ line }),
    } as MessageEvent<string>);
  }
}

interface StreamProps {
  containerId: string;
  onLine: (chunk: string) => void;
}

describe("useLogStream", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("ignores a message from a socket cleaned up after an id change", () => {
    const oldOnLine = vi.fn();
    const newOnLine = vi.fn();
    const view = renderHook(
      ({ containerId, onLine }: StreamProps) => useLogStream({
        containerId,
        agentId: "agent-a",
        enabled: true,
        onLine,
      }),
      {
        initialProps: {
          containerId: "old-id",
          onLine: oldOnLine,
        },
      },
    );

    const oldSocket = FakeWebSocket.instances[0];
    view.rerender({ containerId: "new-id", onLine: newOnLine });
    const newSocket = FakeWebSocket.instances[1];

    expect(oldSocket.close).toHaveBeenCalledOnce();
    act(() => oldSocket.emitLine("late old chunk\n"));
    expect(newOnLine).not.toHaveBeenCalled();

    act(() => newSocket.emitLine("new lifecycle\n"));
    expect(newOnLine).toHaveBeenCalledWith("new lifecycle\n");
  });
});
