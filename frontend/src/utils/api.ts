import { useAuthStore } from "../stores/authStore";
import { useMetricsStore } from "../stores/metricsStore";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

const AGENT_SCOPED_PATHS = ["/api/docker", "/api/process", "/api/logs"];

/**
 * Inject ?agent_id=<selectedAgentId> for routes that target a specific host.
 * No-op if the path already has agent_id, or if no agent is selected.
 */
function withAgentScope(path: string): string {
  const isScoped = AGENT_SCOPED_PATHS.some((p) => path.startsWith(p));
  if (!isScoped) return path;

  const [base, query = ""] = path.split("?");
  const params = new URLSearchParams(query);
  if (params.has("agent_id")) return path;

  const agentId = useMetricsStore.getState().agentId;
  if (!agentId) return path;

  params.set("agent_id", agentId);
  return `${base}?${params.toString()}`;
}

/**
 * Fetch wrapper that auto-injects JWT token, handles 401 refresh,
 * and scopes docker/process/logs calls to the selected agent.
 */
export async function fetchWithAuth(
  path: string,
  options: RequestInit = {}
): Promise<Response> {
  const { accessToken, refresh, logout } = useAuthStore.getState();
  const scopedPath = withAgentScope(path);

  const headers = new Headers(options.headers);
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }

  let res = await fetch(`${BACKEND_URL}${scopedPath}`, { ...options, headers, credentials: "include" });

  // If 401, try refresh once (token mode or cookie mode)
  if (res.status === 401) {
    const refreshed = await refresh();
    if (refreshed) {
      const newToken = useAuthStore.getState().accessToken;
      if (newToken) {
        headers.set("Authorization", `Bearer ${newToken}`);
      }
      res = await fetch(`${BACKEND_URL}${scopedPath}`, { ...options, headers, credentials: "include" });
    } else {
      logout();
    }
  }

  return res;
}
