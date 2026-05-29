import { useMetricsStore } from "../stores/metricsStore";
import { useThresholdsStore } from "../stores/thresholdsStore";
import { deriveAlerts, type Alert } from "../lib/alerts";

// Active = threshold-crossing alerts for the current snapshot, excluding any
// still inside their mute window. Sorted by severity then value (in deriveAlerts).
export function useActiveAlerts(): Alert[] {
  const current = useMetricsStore((s) => s.current);
  const thresholds = useThresholdsStore((s) => s.thresholds);
  const muted = useThresholdsStore((s) => s.muted);
  if (!current) return [];
  // eslint-disable-next-line react-hooks/purity
  const nowMs = Date.now();
  return deriveAlerts(current, thresholds).filter((a) => {
    const until = muted[a.id];
    return !(until && until > nowMs);
  });
}
