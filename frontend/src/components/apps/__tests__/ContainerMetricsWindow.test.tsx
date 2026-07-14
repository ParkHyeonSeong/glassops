import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMetricsStore } from "../../../stores/metricsStore";
import {
  deferred,
  jsonResponse,
  makeContainer,
  makeMetricSnapshot,
} from "../../../test/fixtures";
import { fetchWithAuth } from "../../../utils/api";
import { _resetServerClockForTest } from "../../../utils/serverClock";
import ContainerMetricsWindow from "../ContainerMetricsWindow";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));
vi.mock("recharts", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    AreaChart: Pass,
    ResponsiveContainer: Pass,
    Area: () => null,
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
  };
});

describe("ContainerMetricsWindow", () => {
  beforeEach(() => {
    _resetServerClockForTest();
    vi.mocked(fetchWithAuth).mockReset();
    vi.spyOn(Date, "now").mockReturnValue(2_000);
    useMetricsStore.setState({
      agents: {},
      agentIds: [],
      selectedAgentId: null,
      connected: true,
      current: null,
      history: [],
      agentId: null,
    });
  });

  afterEach(() => {
    _resetServerClockForTest();
    vi.unstubAllGlobals();
  });

  it("merges a live push that arrives before history resolves", async () => {
    const history = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValue(history.promise);

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 1,
          containers: [makeContainer({ name: "worker", cpu_percent: 10 })],
        }),
      );
    });

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 2,
          containers: [makeContainer({ name: "worker", cpu_percent: 30 })],
        }),
      );
    });

    await act(async () => {
      history.resolve(jsonResponse({
        metrics: [{
          t: 1,
          cpu: 10,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await history.promise;
    });

    expect(await screen.findByText(/avg 20\.0% · peak 30\.0%/)).toBeInTheDocument();
  });

  it("keeps current live samples when history loading fails", async () => {
    const history = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValue(history.promise);

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 1,
          containers: [makeContainer({ name: "worker" })],
        }),
      );
    });
    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 2,
          containers: [makeContainer({ name: "worker", cpu_percent: 30 })],
        }),
      );
    });
    expect(screen.getByText(/avg 30\.0% · peak 30\.0%/)).toBeInTheDocument();

    await act(async () => {
      history.reject(new Error("network unavailable"));
      await history.promise.catch(() => undefined);
    });

    expect(await screen.findByText("Failed to load history")).toBeInTheDocument();
    expect(screen.getByText(/avg 30\.0% · peak 30\.0%/)).toBeInTheDocument();
  });

  it("ignores an older range response that resolves last", async () => {
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 1,
          containers: [makeContainer({ name: "worker" })],
        }),
      );
    });
    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [{
          t: 2,
          cpu: 50,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await fiveMinutes.promise;
    });
    expect(await screen.findByText(/avg 50\.0% · peak 50\.0%/)).toBeInTheDocument();

    await act(async () => {
      oneHour.resolve(jsonResponse({
        metrics: [{
          t: 1,
          cpu: 10,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await oneHour.promise;
    });
    expect(screen.getByText(/avg 50\.0% · peak 50\.0%/)).toBeInTheDocument();
    expect(screen.queryByText(/avg 10\.0% · peak 10\.0%/)).not.toBeInTheDocument();
  });

  it("does not reuse history when returning to the same range activation", async () => {
    const firstOneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    const secondOneHour = deferred<Response>();
    let oneHourRequests = 0;
    vi.mocked(fetchWithAuth).mockImplementation((path) => {
      if (path.includes("duration=5m")) return fiveMinutes.promise;
      oneHourRequests += 1;
      return oneHourRequests === 1 ? firstOneHour.promise : secondOneHour.promise;
    });
    vi.mocked(Date.now).mockReturnValue(4_000_000);

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 4_000,
          containers: [makeContainer({ name: "worker" })],
        }),
      );
    });
    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);

    await act(async () => {
      firstOneHour.resolve(jsonResponse({
        metrics: [{
          t: 1_000,
          cpu: 10,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await firstOneHour.promise;
    });
    expect(await screen.findByText(/avg 10\.0% · peak 10\.0%/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "5m" }));
    vi.mocked(Date.now).mockReturnValue(5_000_000);
    fireEvent.click(screen.getByRole("button", { name: "1h" }));

    expect(screen.queryByText(/avg 10\.0% · peak 10\.0%/)).not.toBeInTheDocument();
    expect(screen.getByText("Loading...")).toBeInTheDocument();

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [{
          t: 4_900,
          cpu: 20,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await fiveMinutes.promise;
    });
    expect(screen.queryByText(/avg 20\.0% · peak 20\.0%/)).not.toBeInTheDocument();
    expect(screen.getByText("Loading...")).toBeInTheDocument();

    await act(async () => {
      secondOneHour.resolve(jsonResponse({
        metrics: [{
          t: 5_000,
          cpu: 50,
          mem: 256,
          mem_limit: 1024,
          vram: 0,
          gpu_util: 0,
          gpu_present: false,
        }],
      }));
      await secondOneHour.promise;
    });
    expect(await screen.findByText(/avg 50\.0% · peak 50\.0%/)).toBeInTheDocument();
    expect(screen.queryByText(/avg 30\.0%/)).not.toBeInTheDocument();
  });

  it("keeps history when a future-skewed live sample arrives", async () => {
    vi.mocked(Date.now).mockReturnValue(10_000_000);
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [
          { t: 9_760, cpu: 10, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
          { t: 9_880, cpu: 20, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
        ],
      }));
      await fiveMinutes.promise;
    });
    expect(await screen.findByText(/avg 15\.0% · peak 20\.0%/)).toBeInTheDocument();

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 10_299,
          containers: [makeContainer({ name: "worker", cpu_percent: 40 })],
        }),
      );
    });

    expect(screen.getByText(/avg 23\.3% · peak 40\.0%/)).toBeInTheDocument();
  });

  it("merges history with a live sample that leads the clock", async () => {
    vi.mocked(Date.now).mockReturnValue(10_000_000);
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 10_299,
          containers: [makeContainer({ name: "worker", cpu_percent: 40 })],
        }),
      );
    });

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [
          { t: 9_760, cpu: 10, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
        ],
      }));
      await fiveMinutes.promise;
    });

    expect(await screen.findByText(/avg 25\.0% · peak 40\.0%/)).toBeInTheDocument();
  });

  it("appends a normal sample after a future-skewed one", async () => {
    vi.mocked(Date.now).mockReturnValue(10_000_000);
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 10_299,
          containers: [makeContainer({ name: "worker", cpu_percent: 40 })],
        }),
      );
    });
    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 10_010,
          containers: [makeContainer({ name: "worker", cpu_percent: 20 })],
        }),
      );
    });

    expect(screen.getByText(/avg 30\.0% · peak 40\.0%/)).toBeInTheDocument();

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 10_010,
          containers: [makeContainer({ name: "worker", cpu_percent: 70 })],
        }),
      );
    });
    // 저장 dedup(first-wins)이 재수신된 t=10_010(cpu 70)을 버려야 avg가 불변.
    expect(screen.getByText(/avg 30\.0% · peak 40\.0%/)).toBeInTheDocument();
  });

  it("ages out samples when metrics stop arriving", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      vi.mocked(Date.now).mockReturnValue(10_000_000);
      const oneHour = deferred<Response>();
      const fiveMinutes = deferred<Response>();
      vi.mocked(fetchWithAuth).mockImplementation((path) => (
        path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
      ));

      render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
      fireEvent.click(screen.getByRole("button", { name: "5m" }));

      await act(async () => {
        fiveMinutes.resolve(jsonResponse({
          metrics: [
            { t: 9_900, cpu: 10, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
          ],
        }));
        await fiveMinutes.promise;
      });
      expect(await screen.findByText(/avg 10\.0% · peak 10\.0%/)).toBeInTheDocument();

      vi.mocked(Date.now).mockReturnValue(10_360_000);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      expect(screen.queryByText(/avg 10\.0% · peak 10\.0%/)).not.toBeInTheDocument();
      expect(screen.getByText("No data for this range yet.")).toBeInTheDocument();
    } finally {
      cleanup();
      _resetServerClockForTest();
      vi.useRealTimers();
    }
  });

  it("recovers full history once the server clock syncs", async () => {
    // Browser 240s ahead of the server (server now = 10_000). History
    // resolves BEFORE /api/time — nothing may be deleted from storage.
    vi.mocked(Date.now).mockReturnValue(10_240_000);
    const timeResponse = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => timeResponse.promise));
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [
          { t: 9_700, cpu: 10, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
          { t: 9_960, cpu: 30, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
        ],
      }));
      await fiveMinutes.promise;
    });
    // sync 전: 브라우저 기준 cutoff 9_940 → 표시만 잘린다 (저장은 무손실).
    expect(await screen.findByText(/avg 30\.0% · peak 30\.0%/)).toBeInTheDocument();

    await act(async () => {
      timeResponse.resolve(jsonResponse({ timestamp: 10_000 }));
      await timeResponse.promise;
    });
    // sync 후: offset −240s → cutoff 9_700 → 전체 history가 복구된다.
    expect(await screen.findByText(/avg 20\.0% · peak 30\.0%/)).toBeInTheDocument();
  });

  it("converges to the server window when the browser clock lags", async () => {
    // Browser 240s behind the server (server now = 10_000).
    vi.mocked(Date.now).mockReturnValue(9_760_000);
    const timeResponse = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => timeResponse.promise));
    const oneHour = deferred<Response>();
    const fiveMinutes = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "5m" }));

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 9_500,
          containers: [makeContainer({ name: "worker", cpu_percent: 90 })],
        }),
      );
    });
    await act(async () => {
      fiveMinutes.resolve(jsonResponse({
        metrics: [
          { t: 9_700, cpu: 10, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
          { t: 9_960, cpu: 30, mem: 256, mem_limit: 1024, vram: 0, gpu_util: 0, gpu_present: false },
        ],
      }));
      await fiveMinutes.promise;
    });
    // sync 전: 브라우저 기준 cutoff 9_460 → 만료된 t=9_500까지 보인다.
    expect(await screen.findByText(/avg 43\.3% · peak 90\.0%/)).toBeInTheDocument();

    await act(async () => {
      timeResponse.resolve(jsonResponse({ timestamp: 10_000 }));
      await timeResponse.promise;
    });
    // sync 후: cutoff 9_700 → 만료 샘플이 빠지고 정확한 5m 창으로 수렴.
    expect(await screen.findByText(/avg 20\.0% · peak 30\.0%/)).toBeInTheDocument();
  });
});
