import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { deferred, jsonResponse, makeMetricSnapshot } from "../../test/fixtures";
import { fetchWithAuth } from "../../utils/api";
import { useHistoricalMetrics } from "../useHistoricalMetrics";

vi.mock("../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

function Probe({ agentId, range }: { agentId: string | null; range: string }) {
  const metrics = useHistoricalMetrics(agentId, range);
  return <output>{metrics.map((metric) => metric.timestamp).join(",")}</output>;
}

describe("useHistoricalMetrics", () => {
  it("ignores an older range response that resolves last", async () => {
    const fiveMinutes = deferred<Response>();
    const oneHour = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => (
      path.includes("duration=5m") ? fiveMinutes.promise : oneHour.promise
    ));

    const view = render(<Probe agentId="agent-a" range="5m" />);
    view.rerender(<Probe agentId="agent-a" range="1h" />);

    await act(async () => {
      oneHour.resolve(jsonResponse({ metrics: [makeMetricSnapshot({ timestamp: 200 })] }));
      await oneHour.promise;
    });
    expect(screen.getByText("200")).toBeInTheDocument();

    await act(async () => {
      fiveMinutes.resolve(jsonResponse({ metrics: [makeMetricSnapshot({ timestamp: 100 })] }));
      await fiveMinutes.promise;
    });
    expect(screen.getByText("200")).toBeInTheDocument();
    expect(screen.queryByText("100")).not.toBeInTheDocument();
  });
});
