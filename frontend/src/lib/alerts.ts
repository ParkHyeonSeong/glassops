import type { MetricSnapshot } from "../stores/metricsStore";
import { type MetricKey, type Severity, type Threshold, severityFor } from "./thresholds";

export interface Alert {
  id: string;            // "cpu" | "mem" | "disk" | `core:${index}`
  metric: MetricKey;
  label: string;
  value: number;
  threshold: number;     // the crossed bound (warn or crit)
  severity: Severity;    // never "ok" for an emitted alert
  message: string;
  since: number;         // snapshot timestamp (seconds)
}

const RANK: Record<Severity, number> = { ok: 0, warn: 1, crit: 2 };

// Simple threshold-crossing only — no baseline/anomaly math (per spec).
export function deriveAlerts(
  current: MetricSnapshot,
  thresholds: Record<MetricKey, Threshold>,
): Alert[] {
  const since = current.timestamp ?? 0;
  const out: Alert[] = [];

  const push = (id: string, metric: MetricKey, label: string, value: number) => {
    const t = thresholds[metric];
    const sev = severityFor(value, t);
    if (sev === "ok") return;
    const crossed = sev === "crit" ? t.crit : t.warn;
    out.push({
      id, metric, label, value, threshold: crossed, severity: sev, since,
      // severityFor uses >=, so "reached" reads correctly at the exact boundary too.
      message: `${label} ${value.toFixed(0)}% — reached ${crossed}% ${sev === "crit" ? "critical" : "warning"} threshold`,
    });
  };

  push("cpu", "cpu", "CPU", current.cpu?.percent_total ?? 0);
  push("mem", "mem", "Memory", current.memory?.percent ?? 0);
  push("disk", "disk", "Disk", current.disk?.percent ?? 0);
  (current.cpu?.percent_per_core ?? []).forEach((v, i) =>
    push(`core:${i}`, "core", `Core ${i}`, v),
  );

  return out.sort((a, b) => RANK[b.severity] - RANK[a.severity] || b.value - a.value);
}
