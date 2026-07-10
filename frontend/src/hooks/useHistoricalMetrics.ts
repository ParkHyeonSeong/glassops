import { useEffect, useMemo, useState } from "react";
import type { MetricSnapshot } from "../stores/metricsStore";
import { fetchWithAuth } from "../utils/api";

interface HistoricalActivation {
  key: string | null;
}

interface HistoricalResult {
  activation: HistoricalActivation | null;
  metrics: MetricSnapshot[];
}

export function useHistoricalMetrics(
  agentId: string | null,
  range: string,
): MetricSnapshot[] {
  const key = agentId && range !== "live" ? `${agentId}\u0000${range}` : null;
  const activation = useMemo<HistoricalActivation>(() => ({ key }), [key]);
  const [result, setResult] = useState<HistoricalResult>({ activation: null, metrics: [] });

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
            activation,
            metrics: Array.isArray(payload.metrics) ? payload.metrics : [],
          });
        }
      })
      .catch(() => {
        if (!cancelled) setResult({ activation, metrics: [] });
      });

    return () => {
      cancelled = true;
    };
  }, [activation, agentId, key, range]);

  return result.activation === activation ? result.metrics : [];
}
