import { beforeEach, describe, expect, it } from "vitest";
import { makeMetricSnapshot } from "../../test/fixtures";
import { useMetricsStore } from "../metricsStore";

const sample = (id: number, timestamp = id) => makeMetricSnapshot({
  timestamp, sample_id: `raw:${id}`, arrival_seq: id,
});

beforeEach(() => useMetricsStore.setState({
  agents: {}, agentIds: [], selectedAgentId: null, connected: false,
  current: null, history: [], agentId: null,
}));

describe("Live history restoration", () => {
  it("merges delayed history without replacing a newer live reading", () => {
    const store = useMetricsStore.getState();
    store.pushMetrics("local", sample(11));
    store.mergeHistory("local", [sample(9), sample(10)]);
    expect(useMetricsStore.getState().history.map((s) => s.sample_id)).toEqual(["raw:9", "raw:10", "raw:11"]);
    expect(useMetricsStore.getState().current?.sample_id).toBe("raw:11");
  });

  it("deduplicates fetch/WS redelivery and keeps arrival order across clock changes", () => {
    const store = useMetricsStore.getState();
    store.pushMetrics("local", sample(10, 300));
    store.mergeHistory("local", [sample(9, 300), sample(10, 300), sample(11, 100)]);
    store.pushMetrics("local", sample(10, 300));
    expect(useMetricsStore.getState().history.map((s) => s.sample_id)).toEqual(["raw:9", "raw:10", "raw:11"]);
    expect(useMetricsStore.getState().current?.sample_id).toBe("raw:11");
  });

  it("keeps an unstored sample after its durable anchor and isolates agents", () => {
    const store = useMetricsStore.getState();
    store.pushMetrics("remote", sample(20));
    const ephemeral = makeMetricSnapshot({
      timestamp: 1, sample_id: "ephemeral:00000000-0000-4000-8000-000000000001",
      persisted: false, after_seq: 11,
    });
    store.pushMetrics("local", ephemeral);
    store.mergeHistory("local", [sample(10), sample(11)]);
    expect(useMetricsStore.getState().agents.local.current).toEqual(ephemeral);
    expect(useMetricsStore.getState().agents.remote.history).toEqual([sample(20)]);
    expect(useMetricsStore.getState().agentId).toBe("remote");
  });

  it("caps the merged history at 120 and treats empty history as normal", () => {
    const store = useMetricsStore.getState();
    store.pushMetrics("local", sample(150));
    store.mergeHistory("local", Array.from({ length: 150 }, (_, i) => sample(i + 1)));
    store.mergeHistory("local", []);
    expect(useMetricsStore.getState().history).toHaveLength(120);
    expect(useMetricsStore.getState().history[0].sample_id).toBe("raw:31");
    expect(useMetricsStore.getState().current?.sample_id).toBe("raw:150");
  });
});
