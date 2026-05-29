export type Severity = "ok" | "warn" | "crit";
export type MetricKey = "cpu" | "mem" | "disk" | "core";

export interface Threshold {
  warn: number; // percent
  crit: number; // percent
}

export const DEFAULT_THRESHOLDS: Record<MetricKey, Threshold> = {
  cpu: { warn: 80, crit: 95 },
  mem: { warn: 85, crit: 95 },
  disk: { warn: 75, crit: 90 },
  core: { warn: 80, crit: 95 },
};

export function severityFor(value: number, t: Threshold): Severity {
  if (value >= t.crit) return "crit";
  if (value >= t.warn) return "warn";
  return "ok";
}

// Guard against inverted user input (warn must be <= crit).
export function clampThreshold(t: Threshold): Threshold {
  return { warn: Math.min(t.warn, t.crit), crit: Math.max(t.warn, t.crit) };
}
