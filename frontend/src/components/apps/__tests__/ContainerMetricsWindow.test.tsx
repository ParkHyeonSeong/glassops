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
});
