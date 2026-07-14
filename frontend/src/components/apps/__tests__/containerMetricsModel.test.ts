import { describe, expect, it } from "vitest";
import {
  boundContainerSamples,
  constrainContainerSamples,
  containerMetricsKey,
  effectiveSampleTime,
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

  it("does not let a future sample trim the ranged window", () => {
    expect(constrainContainerSamples(
      [sample(9_700, 10), sample(9_800, 20), sample(9_900, 30),
       sample(10_000, 40), sample(10_299, 50)],
      "5m",
      10_000,
    ).map(({ t }) => t)).toEqual([9_700, 9_800, 9_900, 10_000, 10_299]);
  });

  it("keeps a continuously future-skewed 1 Hz stream on canonical-time semantics", () => {
    // agent +299s, 1 Hz: canonical t 9_700..10_299 (600 samples) at serverNow 10_000.
    // The spec explicitly accepts that a 5m window holds up to W+300s of
    // reception-basis data; this test pins that decision.
    const stream = Array.from({ length: 600 }, (_, i) => sample(9_700 + i, i));

    expect(constrainContainerSamples(stream, "5m", 10_000)).toHaveLength(600);
    expect(constrainContainerSamples(stream, "5m", 10_400)).toHaveLength(200);
    expect(constrainContainerSamples(stream, "5m", 10_600)).toEqual([]);
  });

  it("retains future-only samples and handles empty input", () => {
    // 10_300 = 정책 상한(backend가 허용하는 정확히 +300s)의 경계 샘플.
    expect(constrainContainerSamples(
      [sample(10_300, 60), sample(10_299, 50), sample(10_100, 40)],
      "5m",
      10_000,
    ).map(({ t }) => t)).toEqual([10_100, 10_299, 10_300]);

    expect(constrainContainerSamples([], "5m", 10_000)).toEqual([]);
    expect(constrainContainerSamples([], "live", 10_000)).toEqual([]);
  });

  it("clamps effective time to the server clock", () => {
    expect(effectiveSampleTime(10_299, 10_000)).toBe(10_000);
    expect(effectiveSampleTime(9_900, 10_000)).toBe(9_900);
  });

  it("bounds stored samples by dedup and count only, never by time", () => {
    const stale = [sample(10_299, 50), sample(1_000, 10)];
    expect(boundContainerSamples(stale, "5m").map(({ t }) => t))
      .toEqual([1_000, 10_299]);

    // first-wins on raw t: the re-received 10_010 (cpu 70) must be dropped.
    const duplicated = [sample(10_010, 20), sample(10_299, 40), sample(10_010, 70)];
    expect(boundContainerSamples(duplicated, "5m").map(({ t, cpu }) => [t, cpu]))
      .toEqual([[10_010, 20], [10_299, 40]]);

    const overflow = Array.from({ length: 605 }, (_, i) => sample(i + 1, i)).reverse();
    const ranged = boundContainerSamples(overflow, "1h");
    expect(ranged).toHaveLength(600);
    expect(ranged[0]?.t).toBe(6);
    expect(ranged.at(-1)?.t).toBe(605);

    const live = boundContainerSamples(overflow, "live");
    expect(live).toHaveLength(120);
    expect(live[0]?.t).toBe(486);
    expect(live.at(-1)?.t).toBe(605);
  });
});
