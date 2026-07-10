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
