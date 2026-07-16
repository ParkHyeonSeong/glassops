import { describe, expect, it } from "vitest";
import {
  boundContainerSamples,
  collapseByEffectiveTime,
  constrainContainerSamples,
  containerMetricsKey,
  effectiveSampleTime,
  mergeSamplesByIdentity,
  sampleOrigin,
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

const UUID_A = "00000000-0000-4000-8000-00000000000a";

const identified = (t: number, cpu: number, seq: number): ContainerSample => ({
  ...sample(t, cpu),
  sample_id: `raw:${seq}`,
  arrival_seq: seq,
  persisted: true,
});

const ephemeral = (
  t: number,
  cpu: number,
  uuid: string,
  after: number,
): ContainerSample => ({
  ...sample(t, cpu),
  sample_id: `ephemeral:${uuid}`,
  persisted: false,
  after_seq: after,
});

describe("container metrics model", () => {
  it("builds a key from the complete query identity", () => {
    expect(containerMetricsKey("agent-a", "worker", "1h"))
      .toBe("agent-a\u0000worker\u00001h");
  });

  it("keeps live samples received while history is loading", () => {
    expect(mergeSamplesByIdentity(
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

  it("validates identity metadata from the wire", () => {
    expect(sampleOrigin({ sample_id: "raw:7", arrival_seq: 7, persisted: true }))
      .toEqual({ sample_id: "raw:7", arrival_seq: 7, persisted: true });
    // REST raw entries omit persisted — normalized to true.
    expect(sampleOrigin({ sample_id: "raw:7", arrival_seq: 7 }))
      .toEqual({ sample_id: "raw:7", arrival_seq: 7, persisted: true });
    expect(sampleOrigin({
      sample_id: `ephemeral:${UUID_A}`, persisted: false, after_seq: 5,
    })).toEqual({ sample_id: `ephemeral:${UUID_A}`, persisted: false, after_seq: 5 });

    // Everything below is forgeable through a pre-identity backend that
    // relays agent dicts verbatim — dropped WHOLESALE (persisted included),
    // the sample degrades to legacy semantics.
    const unsafe = Number.MAX_SAFE_INTEGER + 1;
    expect(sampleOrigin({ sample_id: `raw:${unsafe}`, arrival_seq: unsafe })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:not-an-id", arrival_seq: 7 })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:5", arrival_seq: 7 })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:0", arrival_seq: 0 })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:-1", arrival_seq: -1 })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:7", arrival_seq: "7" })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:7", arrival_seq: 7, persisted: false })).toEqual({});
    expect(sampleOrigin({ sample_id: "raw:7", arrival_seq: 7, after_seq: 1 })).toEqual({});
    expect(sampleOrigin({ sample_id: "ephemeral:", persisted: false, after_seq: 0 })).toEqual({});
    expect(sampleOrigin({
      sample_id: `ephemeral:${UUID_A}`, persisted: true, after_seq: 0,
    })).toEqual({});
    expect(sampleOrigin({
      sample_id: `ephemeral:${UUID_A}`, arrival_seq: 3, persisted: false, after_seq: 0,
    })).toEqual({});
    expect(sampleOrigin({ sample_id: `ephemeral:${UUID_A}`, persisted: false })).toEqual({});
    expect(sampleOrigin({ sample_id: "t:100", arrival_seq: 1 })).toEqual({});
    expect(sampleOrigin({})).toEqual({});
  });

  it("dedups the same persisted row across fetch and live", () => {
    // Row 7 arrived over the WS first, then again inside the fetch response.
    const fetched = [identified(100, 10, 5), identified(102, 30, 7)];
    const existing = [identified(101, 20, 6), identified(102, 30, 7)];

    const merged = mergeSamplesByIdentity(fetched, existing);
    expect(merged.map((s) => s.arrival_seq)).toEqual([5, 6, 7]);
  });

  it("keeps distinct samples that share a timestamp", () => {
    const fetched = [identified(100, 20, 5)];
    const existing = [identified(100, 70, 9)];

    expect(mergeSamplesByIdentity(fetched, existing).map((s) => [s.t, s.cpu]))
      .toEqual([[100, 20], [100, 70]]);
  });

  it("orders shuffled fetch/live input by durable arrival", () => {
    // live seq 11 landed while the fetch (rows 10 and 12) was in flight;
    // the merged order must be the backend arrival order, not fetched-first.
    const fetched = [identified(200, 10, 10), identified(50, 30, 12)];
    const existing = [identified(201, 20, 11)];

    expect(mergeSamplesByIdentity(fetched, existing).map((s) => s.arrival_seq))
      .toEqual([10, 11, 12]);
  });

  it("interleaves ephemerals into the arrival order by after_seq", () => {
    // (35,1) < (38,0) < (40,0): the ephemeral that followed id 35 sorts
    // between the downsampled block and the later durables — merge, cap
    // eviction, and collapse all share this one rank order.
    const fetched = [sample(60, 1), sample(120, 2), identified(400, 3, 40)];
    const existing = [ephemeral(390, 9, UUID_A, 35), identified(395, 4, 38)];

    expect(mergeSamplesByIdentity(fetched, existing).map((s) => s.sample_id ?? "legacy"))
      .toEqual(["legacy", "legacy", `ephemeral:${UUID_A}`, "raw:38", "raw:40"]);
  });

  it("lets a fresh ephemeral beat older fetched history at the same X", () => {
    // 리뷰 Important 2 시나리오: fetch 진행 중(버퍼 빈 상태)에 저장 실패
    // live sample이 도착(backend가 마지막으로 발급한 id 5 → after_seq 5).
    // 나중에 resolve된 History의 seq 5 행이 같은 X에 겹쳐도, 사용자는
    // 과거 History 값이 아니라 최신 live 판독을 봐야 한다.
    const fetched = [identified(10_299, 40, 5)];
    const existing = [ephemeral(10_295, 90, UUID_A, 5)];

    const merged = mergeSamplesByIdentity(fetched, existing);
    expect(merged.map((s) => s.cpu)).toEqual([40, 90]); // (5,0) then (5,1)
    expect(collapseByEffectiveTime(merged, 10_000).map((s) => s.cpu)).toEqual([90]);
  });

  it("bounds stored samples by identity dedup", () => {
    // Same DB row delivered twice: first wins. A distinct row at the same t
    // survives (approved same-t policy P1).
    const doubled = [identified(100, 20, 5), identified(100, 20, 5), identified(100, 70, 9)];
    expect(boundContainerSamples(doubled, "5m").map((s) => [s.cpu, s.arrival_seq]))
      .toEqual([[20, 5], [70, 9]]);
  });

  it("evicts the oldest durable arrival from a saturated identity buffer on rollback", () => {
    // Identity twin of the legacy saturated-rollback pin above: the cap must
    // evict the oldest ARRIVAL (head), never the just-received corrected sample.
    const future = Array.from({ length: 120 }, (_, i) => identified(10_180 + i, i, 1_000 + i));
    const corrected = identified(10_001, 90, 1_200);

    const bounded = boundContainerSamples([...future, corrected], "live");
    expect(bounded).toHaveLength(120);
    expect(bounded[0]?.arrival_seq).toBe(1_001);
    expect(bounded.at(-1)).toEqual(corrected);
  });

  it("orders identity-bearing samples by arrival rank before the cap", () => {
    // Array position is not a reliable arrival record (REST and WS are
    // independent channels), so bound re-seats ranked samples by rank.
    const shuffled = [identified(300, 3, 30), identified(100, 1, 10), identified(200, 2, 20)];
    expect(boundContainerSamples(shuffled, "5m").map((s) => s.arrival_seq))
      .toEqual([10, 20, 30]);

    // Mixed Mode B: downsampled legacy keeps its head position; only the
    // ranked tail is re-seated.
    const mixed = [sample(60, 1), sample(120, 2), identified(400, 3, 11), identified(399, 4, 10)];
    expect(boundContainerSamples(mixed, "5m").map((s) => s.sample_id ?? "legacy"))
      .toEqual(["legacy", "legacy", "raw:10", "raw:11"]);
  });

  it("evicts the oldest arrival when a late WS sample follows a fetch", () => {
    // r3.6 #2: raw:10 has a timestamp outside the REST range's window, so the
    // fetch (raw:11..610) reflected first and the delayed WS raw:10 lands at
    // the tail. Capping by position would evict the NEWER raw:11 and keep the
    // stale raw:10 — rank ordering evicts raw:10 instead.
    const fetched = Array.from({ length: 600 }, (_, i) => identified(1_000 + i, i, 11 + i));
    const delayed = identified(999, 99, 10);

    const bounded = boundContainerSamples([...fetched, delayed], "1h");
    expect(bounded).toHaveLength(600);
    expect(bounded.map((s) => s.arrival_seq)).not.toContain(10); // oldest arrival evicted
    expect(bounded[0]?.arrival_seq).toBe(11);                    // newer sample survives
    expect(bounded.at(-1)?.arrival_seq).toBe(610);
  });

  it("leaves a metadata-free buffer untouched", () => {
    // Legacy contract: with no identity anywhere, bound never sorts — the
    // array order (reception order) is the only arrival signal there is.
    const legacy = [sample(300, 3), sample(100, 1), sample(200, 2)];
    expect(boundContainerSamples(legacy, "5m").map(({ t }) => t)).toEqual([300, 100, 200]);
  });

  it("keeps a fresh ephemeral alive in a saturated buffer and represents it", () => {
    // r3.2 #1: after a restart the ephemeral's after_seq is seeded from
    // sqlite_sequence, so a store-failed live sample outranks the whole
    // fetched history — it survives the 600 cap (oldest durable evicted) and
    // represents its X.
    const history = Array.from({ length: 600 }, (_, i) => identified(5_000 + i, i, i + 1));
    const fresh = ephemeral(5_600, 90, UUID_A, 600); // after last durable seq 600

    const merged = mergeSamplesByIdentity(history, [fresh]);
    const bounded = boundContainerSamples(merged, "1h");
    expect(bounded).toHaveLength(600);
    expect(bounded.some((s) => s.sample_id === `ephemeral:${UUID_A}`)).toBe(true);
    expect(bounded[0]?.arrival_seq).toBe(2); // oldest durable (seq 1) evicted
    expect(collapseByEffectiveTime([identified(5_600, 10, 600), fresh], 10_000)
      .map((s) => s.cpu)).toEqual([90]); // (600,1) beats (600,0) at the same X
  });

  it("collapses same-X samples to the greatest arrival rank regardless of input order", () => {
    // NTP rollback: seq 9 (t 10_010) arrived AFTER seq 8 (t 10_299) but sits
    // FIRST in this deliberately shuffled input — explicit metadata must win
    // over array position.
    expect(collapseByEffectiveTime(
      [identified(10_010, 90, 9), identified(10_299, 40, 8)],
      10_000,
    ).map((s) => [s.t, s.cpu])).toEqual([[10_010, 90]]);
  });

  it("ranks an ephemeral between its after_seq and the next durable", () => {
    // The ephemeral followed durable seq 5 (after_seq 5), then durable seq 7
    // arrived. Rank order is input-position-independent: even with the
    // ephemeral LAST in the array, (5,1) loses to (7,0) at the same X...
    expect(collapseByEffectiveTime(
      [identified(10_290, 10, 5), identified(10_299, 40, 7), ephemeral(10_295, 90, UUID_A, 5)],
      10_000,
    ).map((s) => s.cpu)).toEqual([40]);

    // ...but beats the durable it directly followed: (5,1) > (5,0).
    expect(collapseByEffectiveTime(
      [identified(10_290, 10, 5), ephemeral(10_295, 90, UUID_A, 5)],
      10_000,
    ).map((s) => s.cpu)).toEqual([90]);
  });

  it("prefers a durable raw sample over a same-X downsampled point", () => {
    // Integer-timestamp agent rolled back >60s: raw t can equal a 1m bucket
    // boundary in 6h/24h/7d mode. The newer real reading represents the X
    // (design §8.2 — accepted Mode B behavior).
    const bucket = sample(9_960, 15);
    const rawReading = identified(9_960, 80, 42);

    expect(collapseByEffectiveTime([bucket, rawReading], 10_000).map((s) => s.cpu))
      .toEqual([80]);
  });

  it("collapses live duplicates only at identical raw t", () => {
    // serverNow=Infinity: no clamping — distinct t keep distinct points
    // (live contract), only exact raw-t duplicates collapse, by rank.
    const result = collapseByEffectiveTime(
      [identified(100, 70, 6), identified(100, 20, 5), identified(101, 30, 7)],
      Number.POSITIVE_INFINITY,
    );
    expect(result.map((s) => [s.t, s.cpu])).toEqual([[100, 70], [101, 30]]);
  });
});
