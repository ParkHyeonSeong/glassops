import type { ReactNode } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useMetricsStore } from "../../../stores/metricsStore";
import {
  deferred,
  jsonResponse,
  makeContainer,
  makeMetricSnapshot,
} from "../../../test/fixtures";
import { fetchWithAuth } from "../../../utils/api";
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
});
