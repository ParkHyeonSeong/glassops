import { describe, expect, it } from "vitest";
import {
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
});
