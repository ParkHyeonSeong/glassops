import { describe, expect, it } from "vitest";
import {
  constrainContainerSamples,
  containerMetricsKey,
  mergeSamplesByTimestamp,
  type ContainerSample,
} from "../containerMetricsModel";

const sample = (t: number, cpu: number): ContainerSample => ({
  t,
  cpu,
  mem: cpu * 10,
  mem_limit: 1000,
  vram: 0,
  gpu_util: 0,
  gpu_present: false,
});

describe("container metrics model", () => {
  it("builds a key from the complete query identity", () => {
    expect(containerMetricsKey("agent-a", "worker", "1h"))
      .toBe("agent-a\u0000worker\u00001h");
  });

  it("keeps live samples received while history is loading", () => {
    expect(mergeSamplesByTimestamp(
      [sample(1, 10)],
      [sample(1, 99), sample(2, 30)],
    )).toEqual([sample(1, 10), sample(2, 30)]);
  });

  it("constrains ranged and live samples to their active window and cap", () => {
    expect(constrainContainerSamples(
      [sample(1_400, 20), sample(1_399, 10)],
      "1h",
      5_000,
    ).map(({ t }) => t)).toEqual([1_400]);

    const ranged = Array.from(
      { length: 603 },
      (_, index) => sample(1_399 + index, index),
    ).reverse();

    const constrainedRange = constrainContainerSamples(ranged, "1h", 5_000);
    expect(constrainedRange).toHaveLength(600);
    expect(constrainedRange[0]?.t).toBe(1_402);
    expect(constrainedRange.at(-1)?.t).toBe(2_001);
    expect(constrainedRange.map(({ t }) => t)).toEqual(
      [...constrainedRange.map(({ t }) => t)].sort((left, right) => left - right),
    );

    const live = Array.from({ length: 130 }, (_, index) => sample(index + 1, index)).reverse();
    const constrainedLive = constrainContainerSamples(live, "live", 5_000);
    expect(constrainedLive).toHaveLength(120);
    expect(constrainedLive[0]?.t).toBe(11);
    expect(constrainedLive.at(-1)?.t).toBe(130);
  });
});
