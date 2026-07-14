import { describe, expect, it } from "vitest";
import {
  boundContainerSamples,
  collapseByEffectiveTime,
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

    // 도착 순서(시간 오름차순) 입력 — constrainContainerSamples는 이제 내부적으로
    // 정렬하지 않으므로, 이 테스트는 입력을 도착 순서 그대로 구성한다.
    const ranged = Array.from(
      { length: 603 },
      (_, index) => sample(1_399 + index, index),
    );

    const constrainedRange = constrainContainerSamples(ranged, "1h", 5_000);
    expect(constrainedRange).toHaveLength(600);
    expect(constrainedRange[0]?.t).toBe(1_402);
    expect(constrainedRange.at(-1)?.t).toBe(2_001);
    // constrain은 정렬하지 않는다 — 비단조 '도착' 순서가 그대로 보존된다.
    const outOfOrder = [sample(1_500, 1), sample(1_450, 2), sample(1_600, 3)];
    expect(constrainContainerSamples(outOfOrder, "1h", 5_000).map(({ t }) => t))
      .toEqual([1_500, 1_450, 1_600]);

    const live = Array.from({ length: 130 }, (_, index) => sample(index + 1, index));
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
    ).map(({ t }) => t)).toEqual([10_300, 10_299, 10_100]);

    expect(constrainContainerSamples([], "5m", 10_000)).toEqual([]);
    expect(constrainContainerSamples([], "live", 10_000)).toEqual([]);
  });

  it("clamps effective time to the server clock", () => {
    expect(effectiveSampleTime(10_299, 10_000)).toBe(10_000);
    expect(effectiveSampleTime(9_900, 10_000)).toBe(9_900);
  });

  it("bounds stored samples by dedup and arrival-order cap only", () => {
    // 시간 절단 없음 + 정렬 없음: 배열 순서(도착 순서) 보존.
    const stale = [sample(10_299, 50), sample(1_000, 10)];
    expect(boundContainerSamples(stale, "5m").map(({ t }) => t))
      .toEqual([10_299, 1_000]);

    // first-wins on raw t: 재수신된 10_010(cpu 70)은 버려진다.
    const duplicated = [sample(10_010, 20), sample(10_299, 40), sample(10_010, 70)];
    expect(boundContainerSamples(duplicated, "5m").map(({ t, cpu }) => [t, cpu]))
      .toEqual([[10_010, 20], [10_299, 40]]);

    // cap은 가장 오래된 '도착'을 축출한다 — t가 아니라.
    const overflow = Array.from({ length: 605 }, (_, i) => sample(i + 1, i)).reverse();
    const ranged = boundContainerSamples(overflow, "1h");
    expect(ranged).toHaveLength(600);
    expect(ranged[0]?.t).toBe(600);
    expect(ranged.at(-1)?.t).toBe(1);

    const live = boundContainerSamples(overflow, "live");
    expect(live).toHaveLength(120);
    expect(live[0]?.t).toBe(120);
    expect(live.at(-1)?.t).toBe(1);
  });

  it("keeps corrected-clock samples when a saturated buffer holds future ones", () => {
    // agent +299s로 live 버퍼 120개가 미래 t로 포화된 뒤 시계가 보정되어
    // t=10_001이 도착 — timestamp 정렬 cap이라면 방금 온 샘플이 최솟값이라
    // 즉시 축출된다. arrival cap은 가장 오래된 도착(10_180)을 대신 축출한다.
    const future = Array.from({ length: 120 }, (_, i) => sample(10_180 + i, i));
    const corrected = sample(10_001, 90);

    const bounded = boundContainerSamples([...future, corrected], "live");
    expect(bounded).toHaveLength(120);
    expect(bounded.at(-1)).toEqual(corrected);
    expect(bounded[0]?.t).toBe(10_181);
  });

  it("collapses clamped chart samples to the latest-arrived reading", () => {
    // 단조 시계: 마지막 도착 = 최대 t — 종전과 동일한 결과.
    expect(collapseByEffectiveTime(
      [sample(9_760, 10), sample(10_100, 40), sample(10_200, 60), sample(10_299, 80)],
      10_000,
    ).map(({ t, cpu }) => [t, cpu])).toEqual([[9_760, 10], [10_299, 80]]);

    // clock rollback: 먼저 도착한 future(t 10_299, cpu 40) 뒤에 보정된
    // t 10_010(cpu 90)이 도착 — 대표값은 최신 '도착'(cpu 90)이어야 한다.
    // raw t 최대(10_299)를 고르면 차트·Tooltip이 과거 값을 보여준다.
    expect(collapseByEffectiveTime(
      [sample(10_299, 40), sample(10_010, 90)],
      10_000,
    ).map(({ t, cpu }) => [t, cpu])).toEqual([[10_010, 90]]);
  });
});
