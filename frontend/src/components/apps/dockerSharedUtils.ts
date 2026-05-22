/* Shared types, hooks, and pure utilities for Docker apps/windows. */

import { useCallback, useState } from "react";
import { fetchWithAuth } from "../../utils/api";

export interface ContainerWindowProps {
  agentId: string;
  containerName: string;
}

export type ContainerAction = "start" | "stop" | "restart";

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

/**
 * Wraps a container action call so logs/metrics windows share loading + error state.
 * The `agentId` is included explicitly in the URL so the call always hits the host
 * the window was opened against — fetchWithAuth would otherwise scope by the currently
 * selected agent, which is wrong once the user switches hosts in the MenuBar.
 */
export function useContainerAction(agentId: string, containerId: string | null) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (action: ContainerAction) => {
    if (!containerId) return;
    setLoading(true);
    setError(null);
    try {
      const url = `/api/docker/containers/${containerId}/action?agent_id=${encodeURIComponent(agentId)}`;
      const res = await fetchWithAuth(url, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || `${action} failed`);
      }
    } catch {
      setError("Failed to connect to backend");
    }
    setLoading(false);
  }, [agentId, containerId]);

  return { loading, error, run, clearError: () => setError(null) };
}
