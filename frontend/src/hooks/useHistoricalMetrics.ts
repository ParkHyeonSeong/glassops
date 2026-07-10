import { useEffect, useState } from "react";
import type { MetricSnapshot } from "../stores/metricsStore";
import { fetchWithAuth } from "../utils/api";

interface HistoricalResult {
  key: string | null;
  metrics: MetricSnapshot[];
}

export function useHistoricalMetrics(
  agentId: string | null,
  range: string,
): MetricSnapshot[] {
  const key = agentId && range !== "live" ? `${agentId}\u0000${range}` : null;
  const [result, setResult] = useState<HistoricalResult>({ key: null, metrics: [] });

  useEffect(() => {
    if (!key || !agentId) return;
    let cancelled = false;

    fetchWithAuth(`/api/metrics/${agentId}/range?duration=${range}`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        if (!cancelled) {
          setResult({
            key,
            metrics: Array.isArray(payload.metrics) ? payload.metrics : [],
          });
        }
      })
      .catch(() => {
        if (!cancelled) setResult({ key, metrics: [] });
      });

    return () => {
      cancelled = true;
    };
  }, [agentId, key, range]);

  return result.key === key ? result.metrics : [];
}
