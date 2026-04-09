import { useAuthStore } from "../stores/authStore";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

/**
 * Fetch wrapper that auto-injects JWT token and handles 401 refresh.
 */
export async function fetchWithAuth(
  path: string,
  options: RequestInit = {}
): Promise<Response> {
  const { accessToken, refresh, logout } = useAuthStore.getState();

  const headers = new Headers(options.headers);
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }

  let res = await fetch(`${BACKEND_URL}${path}`, { ...options, headers, credentials: "include" });

  // If 401, try refresh once (token mode or cookie mode)
  if (res.status === 401) {
    const refreshed = await refresh();
    if (refreshed) {
      const newToken = useAuthStore.getState().accessToken;
      if (newToken) {
        headers.set("Authorization", `Bearer ${newToken}`);
      }
      res = await fetch(`${BACKEND_URL}${path}`, { ...options, headers, credentials: "include" });
    } else {
      logout();
    }
  }

  return res;
}
