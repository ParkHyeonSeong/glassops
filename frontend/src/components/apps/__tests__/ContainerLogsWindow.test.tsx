import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useMetricsStore } from "../../../stores/metricsStore";
import { makeContainer, makeMetricSnapshot } from "../../../test/fixtures";
import ContainerLogsWindow from "../ContainerLogsWindow";

const streamCallbacks = vi.hoisted(
  () => new Map<string, (chunk: string) => void>(),
);

vi.mock("../../../hooks/useLogStream", () => ({
  useLogStream: ({
    containerId,
    onLine,
  }: {
    containerId: string | null;
    onLine: (chunk: string) => void;
  }) => {
    if (containerId) streamCallbacks.set(containerId, onLine);
    return { status: "streaming", error: null };
  },
}));

describe("ContainerLogsWindow", () => {
  beforeEach(() => {
    streamCallbacks.clear();
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

  it("ignores a late chunk from the previous container id", () => {
    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 1,
          containers: [makeContainer({ id: "old-id", name: "worker" })],
        }),
      );
    });

    render(<ContainerLogsWindow agentId="agent-a" containerName="worker" />);

    act(() => streamCallbacks.get("old-id")?.("old lifecycle\n"));
    expect(screen.getByText(/old lifecycle/)).toBeInTheDocument();

    act(() => {
      useMetricsStore.getState().pushMetrics(
        "agent-a",
        makeMetricSnapshot({
          timestamp: 2,
          containers: [makeContainer({ id: "new-id", name: "worker" })],
        }),
      );
    });

    act(() => streamCallbacks.get("old-id")?.("late old chunk\n"));
    expect(screen.queryByText(/late old chunk/)).not.toBeInTheDocument();

    act(() => streamCallbacks.get("new-id")?.("new lifecycle\n"));
    expect(screen.getByText(/new lifecycle/)).toBeInTheDocument();
  });
});
