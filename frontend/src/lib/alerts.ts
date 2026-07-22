import type { MetricSnapshot } from "../stores/metricsStore";
import {
  type AlertMetricKey, type MetricKey, type Severity, type Threshold, severityFor,
} from "./thresholds";

export interface Alert {
  id: AlertMetricKey;    // "cpu" | "mem" | "disk"
  metric: AlertMetricKey;
  label: string;
  value: number;
  threshold: number;     // the crossed bound (warn or crit)
  severity: Severity;    // never "ok" for an emitted alert
  message: string;
  since: number;         // snapshot timestamp (seconds)
}

const RANK: Record<Severity, number> = { ok: 0, warn: 1, crit: 2 };

// Simple threshold-crossing only — no baseline/anomaly math (per spec).
//
// Only aggregate metrics alert. Per-core CPU is deliberately excluded: a 32-core
// box with every core briefly pegged used to emit 32 core:N alerts on top of the
// CPU one, flooding the banner (which headlines alerts[0] and counts alerts.length)
// even when cpu.percent_total was healthy. thresholds.core survives as the Cores
// tab's display threshold — see CoreCell — and is never read here.
export function deriveAlerts(
  current: MetricSnapshot,
  thresholds: Record<MetricKey, Threshold>,
): Alert[] {
  const since = current.timestamp ?? 0;
  const out: Alert[] = [];

  const push = (metric: AlertMetricKey, label: string, value: number) => {
    const t = thresholds[metric];
    const sev = severityFor(value, t);
    if (sev === "ok") return;
    const crossed = sev === "crit" ? t.crit : t.warn;
    out.push({
      id: metric, metric, label, value, threshold: crossed, severity: sev, since,
      // severityFor uses >=, so "reached" reads correctly at the exact boundary too.
      message: `${label} ${value.toFixed(0)}% — reached ${crossed}% ${sev === "crit" ? "critical" : "warning"} threshold`,
    });
  };

  push("cpu", "CPU", current.cpu?.percent_total ?? 0);
  push("mem", "Memory", current.memory?.percent ?? 0);
  push("disk", "Disk", current.disk?.percent ?? 0);

  return out.sort((a, b) => RANK[b.severity] - RANK[a.severity] || b.value - a.value);
}

/**
 * Toast shape for a derived alert. Desktop toasts and the System Monitor banner
 * are both fed from deriveAlerts(), so their thresholds cannot drift apart.
 * The key carries the severity so a warn->crit escalation is not swallowed by
 * useAlertStore's per-key 30s cooldown.
 */
export function toastForAlert(a: Alert): {
  type: "warning" | "error";
  message: string;
  key: string;
} {
  return {
    type: a.severity === "crit" ? "error" : "warning",
    message: a.message,
    key: `${a.id}-${a.severity}`,
  };
}
