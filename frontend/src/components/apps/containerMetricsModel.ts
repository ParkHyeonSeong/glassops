export type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";

export interface ContainerSample {
  t: number;
  cpu: number;
  mem: number;
  mem_limit: number;
  vram: number;
  gpu_util: number;
  gpu_present: boolean;
}

export const RANGE_WINDOW_SEC: Record<Exclude<TimeRange, "live">, number> = {
  "5m": 300,
  "1h": 3600,
  "6h": 21600,
  "24h": 86400,
  "7d": 604800,
};
const LIVE_MAX_SAMPLES = 120;
const RANGED_MAX_SAMPLES = 600;

export function containerMetricsKey(
  agentId: string,
  containerName: string,
  range: TimeRange,
): string {
  return `${agentId}\u0000${containerName}\u0000${range}`;
}

/**
 * Dedup merge keyed on canonical t: fetched history wins over a live sample
 * with the same timestamp. Output approximates ARRIVAL order — fetched block
 * first, then surviving live samples in reception order. This is an
 * approximation: the fetch resolves after any live pushes received while it
 * was in flight, so those survivors sit after the fetched block even though
 * they arrived first. Fetched-first is load-bearing for the arrival cap
 * (boundContainerSamples must evict fetched-history head, not fresh live
 * survivors); exact per-sample arrival metadata is a known follow-up.
 * Storage order is never timestamp-sorted (see boundContainerSamples).
 */
export function mergeSamplesByTimestamp(
  fetched: ContainerSample[],
  existing: ContainerSample[],
): ContainerSample[] {
  const fetchedTimestamps = new Set(fetched.map((sample) => sample.t));
  return [
    ...fetched,
    ...existing.filter((sample) => !fetchedTimestamps.has(sample.t)),
  ];
}

export function effectiveSampleTime(t: number, serverNow: number): number {
  return Math.min(t, serverNow);
}

/**
 * Storage bound: first-wins dedup on raw t + arrival-order count cap. Never
 * trims by time and never sorts — the array order IS the arrival record.
 * Capping by arrival evicts the oldest RECEIVED sample, so an agent clock
 * that steps backwards (NTP correction) can never have its fresh readings
 * evicted by older-but-future-stamped ones already in the buffer. First-wins
 * keeps fetched history authoritative over a same-t live sample (the fetch
 * path merges fetched first). Time-window membership is a render-time
 * concern (constrainContainerSamples).
 */
export function boundContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
): ContainerSample[] {
  const seen = new Set<number>();
  const deduped: ContainerSample[] = [];
  for (const sample of samples) {
    if (seen.has(sample.t)) continue;
    seen.add(sample.t);
    deduped.push(sample);
  }
  const cap = range === "live" ? LIVE_MAX_SAMPLES : RANGED_MAX_SAMPLES;
  return deduped.slice(-cap);
}

/**
 * Display window: membership uses effective time (min(t, serverNow)); the
 * raw canonical t stays untouched. Output preserves arrival order so the
 * chart layer can pick latest-arrival representatives — drawing order is
 * the chart layer's concern. Ingest allows up to +300s of future skew as
 * canonical data, so the window is defined in canonical time and may hold
 * up to W+300s of reception-basis data (spec r6, explicit choice).
 */
export function constrainContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
  serverNow: number,
): ContainerSample[] {
  if (range === "live") return samples.slice(-LIVE_MAX_SAMPLES);

  const cutoff = serverNow - RANGE_WINDOW_SEC[range];
  return samples
    .filter((sample) => effectiveSampleTime(sample.t, serverNow) >= cutoff)
    .slice(-RANGED_MAX_SAMPLES);
}

/**
 * Chart-only view: samples whose effective time collapses to the same X
 * position are represented by the LATEST-ARRIVED one — input order is the
 * arrival record (a contract with the storage layer), because under a
 * clock rollback the largest raw t is an OLDER reading, not the newest.
 * Recharts' axis tooltip picks the first payload matching a label, so
 * overlaps must collapse to a single point. Output is sorted by effective
 * time (ascending X) for path drawing. Statistics must keep using the
 * full window data, not this view.
 */
export function collapseByEffectiveTime(
  samples: ContainerSample[],
  serverNow: number,
): ContainerSample[] {
  const byEffectiveTime = new Map<number, ContainerSample>();
  for (const sample of samples) {
    byEffectiveTime.set(effectiveSampleTime(sample.t, serverNow), sample);
  }
  return [...byEffectiveTime.values()].sort(
    (left, right) =>
      effectiveSampleTime(left.t, serverNow) - effectiveSampleTime(right.t, serverNow),
  );
}
