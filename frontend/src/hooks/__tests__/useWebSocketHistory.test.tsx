import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAlertStore } from "../../stores/alertStore";
import { useAuthStore } from "../../stores/authStore";
import { useMetricsStore } from "../../stores/metricsStore";
import { deferred, jsonResponse, makeMetricSnapshot } from "../../test/fixtures";
import { fetchWithAuth } from "../../utils/api";
import { useWebSocket } from "../useWebSocket";

vi.mock("../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

class Socket {
  static instances: Socket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { Socket.instances.push(this); }
  open() { this.onopen?.(); }
  close(code = 1000) { this.onclose?.({ code } as CloseEvent); }
  sample(agent: string, id: number) {
    this.onmessage?.({ data: JSON.stringify({ agent_id: agent, metrics: metric(id) }) } as MessageEvent);
  }
}

const metric = (id: number) => makeMetricSnapshot({ timestamp: id, sample_id: `raw:${id}`, arrival_seq: id });
const history = (agent: string, ids: number[]) => jsonResponse({ agent_id: agent, metrics: ids.map(metric) });
const ids = (agent = "local") => useMetricsStore.getState().agents[agent]?.history.map((s) => s.arrival_seq);
function Probe() { useWebSocket(); return null; }

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0);
  vi.stubGlobal("WebSocket", Socket);
  Socket.instances = [];
  vi.mocked(fetchWithAuth).mockReset();
  useAuthStore.setState({ isAuthenticated: true, email: "admin@example.com", accessToken: "token" });
  useMetricsStore.setState({ agents: {}, agentIds: [], selectedAgentId: null, connected: false,
    current: null, history: [], agentId: null });
  useAlertStore.setState({ alerts: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("WebSocket history hydration", () => {
  it("requests history once per agent and preserves pushes received while loading", async () => {
    const request = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValue(request.promise);
    render(<Probe />);
    act(() => { Socket.instances[0].open(); Socket.instances[0].sample("local", 11); });
    act(() => Socket.instances[0].sample("local", 12));
    await act(async () => { request.resolve(history("local", [9, 10, 11])); });
    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
    expect(fetchWithAuth).toHaveBeenCalledWith("/api/metrics/local/history?limit=120", expect.anything());
    expect(ids()).toEqual([9, 10, 11, 12]);
    expect(useMetricsStore.getState().current?.arrival_seq).toBe(12);
  });

  it("keeps each agent's restored samples separate", async () => {
    vi.mocked(fetchWithAuth).mockImplementation(async (path) => (
      path.includes("/remote/") ? history("remote", [20, 21]) : history("local", [9, 10])
    ));
    render(<Probe />);
    await act(async () => {
      Socket.instances[0].open();
      Socket.instances[0].sample("local", 11);
      Socket.instances[0].sample("remote", 22);
    });
    expect(ids()).toEqual([9, 10, 11]);
    expect(ids("remote")).toEqual([20, 21, 22]);
  });

  it("ignores an old connection's response and messages after reconnect", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<Probe />);
    act(() => { Socket.instances[0].open(); Socket.instances[0].sample("local", 11); });
    act(() => Socket.instances[0].close(1006));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    act(() => { Socket.instances[1].open(); Socket.instances[1].sample("local", 20); });
    await act(async () => { first.resolve(history("local", [1])); second.resolve(history("local", [18, 19])); });
    act(() => Socket.instances[0].sample("local", 999));
    expect(ids()).toEqual([11, 18, 19, 20]);
    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
  });

  it.each(["logout", "unmount"])("ignores history finishing after %s", async (action) => {
    const request = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValue(request.promise);
    const view = render(<Probe />);
    act(() => { Socket.instances[0].open(); Socket.instances[0].sample("local", 11); });
    act(() => {
      if (action === "logout") useAuthStore.setState({ isAuthenticated: false, email: null });
      else view.unmount();
    });
    await act(async () => { request.resolve(history("local", [1])); });
    expect(ids()).toEqual([11]);
  });

  it("keeps Live running on lookup failure and retries only on reconnect", async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({}, 503)).mockResolvedValueOnce(history("local", []));
    render(<Probe />);
    await act(async () => { Socket.instances[0].open(); Socket.instances[0].sample("local", 11); });
    act(() => Socket.instances[0].sample("local", 12));
    expect(ids()).toEqual([11, 12]);
    expect(useAlertStore.getState().alerts).toHaveLength(1);
    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
    act(() => Socket.instances[0].close(1006));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    await act(async () => { Socket.instances[1].open(); Socket.instances[1].sample("local", 13); });
    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    expect(ids()).toEqual([11, 12, 13]);
  });
});
