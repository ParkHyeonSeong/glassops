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

const RANGE_WINDOW_SEC: Record<Exclude<TimeRange, "live">, number> = {
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

export function constrainContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
  now: number,
): ContainerSample[] {
  const sorted = [...samples].sort((left, right) => left.t - right.t);
  if (range === "live") return sorted.slice(-LIVE_MAX_SAMPLES);

  const newestTimestamp = sorted[sorted.length - 1]?.t ?? now;
  const cutoff = Math.max(newestTimestamp, now) - RANGE_WINDOW_SEC[range];
  return sorted
    .filter((sample) => sample.t >= cutoff)
    .slice(-RANGED_MAX_SAMPLES);
}
