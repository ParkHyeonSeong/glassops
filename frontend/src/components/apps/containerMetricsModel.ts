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

export function mergeSamplesByTimestamp(
  fetched: ContainerSample[],
  existing: ContainerSample[],
): ContainerSample[] {
  const fetchedTimestamps = new Set(fetched.map((sample) => sample.t));
  return [
    ...fetched,
    ...existing.filter((sample) => !fetchedTimestamps.has(sample.t)),
  ].sort((left, right) => left.t - right.t);
}

export function effectiveSampleTime(t: number, serverNow: number): number {
  return Math.min(t, serverNow);
}

/**
 * Storage bound: first-wins dedup on raw t + sort + count cap. Never trims
 * by time — the server clock may not be synced yet when data arrives, and
 * a wrong-clock trim would be irreversible. First-wins preserves both
 * existing precedence rules: the fetch path merges with fetched samples
 * first, and the live path appends the new sample after the existing ones.
 * Time-window membership is a render-time concern (constrainContainerSamples).
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
  deduped.sort((left, right) => left.t - right.t);
  const cap = range === "live" ? LIVE_MAX_SAMPLES : RANGED_MAX_SAMPLES;
  return deduped.slice(-cap);
}

/**
 * Display window: membership and chart position use effective time
 * (min(t, serverNow)); the raw canonical t stays untouched — it is the
 * history/live merge identity. Ingest allows up to +300s of future skew
 * as canonical data, so the window is defined in canonical time and may
 * hold up to W+300s of reception-basis data (spec r5, explicit choice).
 */
export function constrainContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
  serverNow: number,
): ContainerSample[] {
  const sorted = [...samples].sort((left, right) => left.t - right.t);
  if (range === "live") return sorted.slice(-LIVE_MAX_SAMPLES);

  const cutoff = serverNow - RANGE_WINDOW_SEC[range];
  return sorted
    .filter((sample) => effectiveSampleTime(sample.t, serverNow) >= cutoff)
    .slice(-RANGED_MAX_SAMPLES);
}

/**
 * Chart-only view: samples whose effective time collapses to the same X
 * position (future samples clamped to serverNow) are represented by the
 * one with the largest raw t — the newest reading. Recharts' axis tooltip
 * picks the first payload matching a label, so leaving the overlap in
 * place would show the OLDEST overlapped value at the right edge.
 * Statistics must keep using the full window data, not this view.
 */
export function collapseByEffectiveTime(
  samples: ContainerSample[],
  serverNow: number,
): ContainerSample[] {
  const byEffectiveTime = new Map<number, ContainerSample>();
  for (const sample of samples) {
    const key = effectiveSampleTime(sample.t, serverNow);
    const current = byEffectiveTime.get(key);
    if (!current || sample.t >= current.t) byEffectiveTime.set(key, sample);
  }
  return [...byEffectiveTime.values()].sort((left, right) => left.t - right.t);
}
