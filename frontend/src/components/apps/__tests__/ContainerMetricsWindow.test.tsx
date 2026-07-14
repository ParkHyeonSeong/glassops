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
  const AreaChart = ({ children, data }: { children?: ReactNode; data?: unknown }) => (
    <div data-testid="area-chart" data-points={JSON.stringify(data)}>{children}</div>
  );
  const XAxis = ({ type, domain }: { type?: string; domain?: unknown }) => (
    <div data-testid="x-axis" data-type={type} data-domain={JSON.stringify(domain)} />
  );
  return {
    AreaChart,
    ResponsiveContainer: Pass,
    Area: () => null,
    XAxis,
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
          t: 4_000,
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

    // 두 샘플(t 10_299 선착, t 10_010 후착) 모두 effective X=10_000으로
    // collapse — 대표값은 최신 도착(cpu 20)이어야 한다. raw t 최대를 고르면
    // 차트·Tooltip(cpu 40)과 통계(avg 30.0%)가 어긋난다.
    const charts = screen.getAllByTestId("area-chart");
    const cpuPoints = JSON.parse(charts[0]?.dataset.points ?? "[]") as
      { t: number; value: number }[];
    expect(cpuPoints).toEqual([{ t: 10_000, value: 20 }]);
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

  it("plots ranged charts on a numeric server-clock axis", async () => {
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
          { t: 9_760, cpu: 10, mem: 256, mem_limit: 1024, vram: 100, gpu_util: 5, gpu_present: true },
        ],
      }));
      await fiveMinutes.promise;
    });
    // 지속적으로 빠른 agent: 미래 샘플 3개가 모두 serverNow(10_000)로 클램프된다.
    for (const [timestamp, cpu] of [[10_100, 40], [10_200, 60], [10_299, 80]] as const) {
      act(() => {
        useMetricsStore.getState().pushMetrics(
          "agent-a",
          makeMetricSnapshot({
            timestamp,
            containers: [makeContainer({
              name: "worker",
              cpu_percent: cpu,
              gpu: { vram_bytes: 200, gpu_util: 7 },
            })],
          }),
        );
      });
    }

    const axes = screen.getAllByTestId("x-axis");
    expect(axes).toHaveLength(4); // CPU, Memory, GPU Util, GPU VRAM
    for (const axis of axes) {
      expect(axis.dataset.type).toBe("number");
      expect(JSON.parse(axis.dataset.domain ?? "null")).toEqual([9_700, 10_000]);
    }

    const charts = screen.getAllByTestId("area-chart");
    expect(charts).toHaveLength(4);
    for (const chart of charts) {
      const points = JSON.parse(chart.dataset.points ?? "[]") as { t: number }[];
      // raw t 9_760은 그대로, 겹친 미래 샘플 3개는 하나의 클램프 점(10_000)으로
      // 대표된다. 정확한 배열 단언이라 미래 샘플이 누락([9_760])되거나 겹친
      // 점이 남으면([9_760, 10_000, 10_000, 10_000]) 실패한다.
      expect(points.map(({ t }) => t)).toEqual([9_760, 10_000]);
    }

    // 같은 X로 겹친 샘플의 차트 대표값은 최신 '도착' 샘플이다 — 이 단조 입력
    // 에서는 마지막 도착이 raw t 최대(cpu 80)와 일치한다. rollback 반례(도착이
    // t 역순)는 appends 테스트가 고정한다.
    // Recharts axis Tooltip이 동일 label의 첫 payload를 선택하므로, 대표값이
    // 최신이 아니면 Tooltip이 오래된 값을 보여 avg/peak와 어긋난다.
    const cpuPoints = JSON.parse(charts[0]?.dataset.points ?? "[]") as
      { t: number; value: number }[];
    expect(cpuPoints).toEqual([
      { t: 9_760, value: 10 },
      { t: 10_000, value: 80 },
    ]);

    // 통계는 collapse 전의 전체 창 데이터로 계산된다: (10+40+60+80)/4 = 47.5.
    expect(screen.getByText(/avg 47\.5% · peak 80\.0%/)).toBeInTheDocument();
  });

  it("keeps future-skewed live samples distinct in live mode", async () => {
    vi.mocked(Date.now).mockReturnValue(10_000_000);
    const oneHour = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation(() => oneHour.promise);

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));

    for (const [timestamp, cpu] of [[10_100, 40], [10_200, 60], [10_299, 80]] as const) {
      act(() => {
        useMetricsStore.getState().pushMetrics(
          "agent-a",
          makeMetricSnapshot({
            timestamp,
            containers: [makeContainer({ name: "worker", cpu_percent: cpu })],
          }),
        );
      });
    }

    // Live 축은 numeric + dataMin~dataMax — ranged domain이 잘못 적용되면
    // raw 미래 점 3개가 domain 밖으로 밀려 그래프에서 숨겨질 수 있다.
    const axes = screen.getAllByTestId("x-axis");
    expect(axes).toHaveLength(2); // CPU + Memory (GPU 없음)
    for (const axis of axes) {
      expect(axis.dataset.type).toBe("number");
      expect(JSON.parse(axis.dataset.domain ?? "null")).toEqual(["dataMin", "dataMax"]);
    }

    // Live 차트는 클램프·collapse 없이 raw t 세 점을 모두 유지해야 한다 —
    // 시계 빠른 agent에서 Live 그래프가 한 점으로 붕괴하면 안 된다.
    const charts = screen.getAllByTestId("area-chart");
    const cpuPoints = JSON.parse(charts[0]?.dataset.points ?? "[]") as
      { t: number; value: number }[];
    expect(cpuPoints).toEqual([
      { t: 10_100, value: 40 },
      { t: 10_200, value: 60 },
      { t: 10_299, value: 80 },
    ]);
    expect(screen.getByText(/avg 60\.0% · peak 80\.0%/)).toBeInTheDocument();
  });

  it("draws corrected-clock live samples in time order after future ones", async () => {
    vi.mocked(Date.now).mockReturnValue(10_000_000);
    const oneHour = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation(() => oneHour.promise);

    render(<ContainerMetricsWindow agentId="agent-a" containerName="worker" />);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));

    for (const [timestamp, cpu] of [[10_297, 40], [10_298, 60], [10_299, 80], [10_001, 90]] as const) {
      act(() => {
        useMetricsStore.getState().pushMetrics(
          "agent-a",
          makeMetricSnapshot({
            timestamp,
            containers: [makeContainer({ name: "worker", cpu_percent: cpu })],
          }),
        );
      });
    }

    // 보정된 t=10_001은 버려지지 않고, live 차트는 raw t 오름차순으로 그린다.
    const charts = screen.getAllByTestId("area-chart");
    const cpuPoints = JSON.parse(charts[0]?.dataset.points ?? "[]") as
      { t: number; value: number }[];
    expect(cpuPoints).toEqual([
      { t: 10_001, value: 90 },
      { t: 10_297, value: 40 },
      { t: 10_298, value: 60 },
      { t: 10_299, value: 80 },
    ]);
    expect(screen.getByText(/avg 67\.5% · peak 90\.0%/)).toBeInTheDocument();
  });
});
