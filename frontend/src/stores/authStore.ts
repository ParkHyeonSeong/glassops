import { create } from "zustand";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

// Restore session from sessionStorage on load
function loadSession() {
  try {
    const data = sessionStorage.getItem("glassops_auth");
    if (data) return JSON.parse(data);
  } catch { /* ignore */ }
  return null;
}

function saveSession(state: { email: string; accessToken: string; refreshToken: string }) {
  try {
    sessionStorage.setItem("glassops_auth", JSON.stringify(state));
  } catch { /* ignore */ }
}

function clearSession() {
  try {
    sessionStorage.removeItem("glassops_auth");
  } catch { /* ignore */ }
}

const saved = loadSession();

interface AuthStore {
  isAuthenticated: boolean;
  email: string | null;
  accessToken: string | null;
  refreshToken: string | null;
  requiresTotp: boolean;
  mustChangePassword: boolean;
  cookieMode: boolean;

  login: (email: string, password: string, totpCode?: string) => Promise<{ ok: boolean; requiresTotp?: boolean; error?: string }>;
  logout: () => void;
  refresh: () => Promise<boolean>;
  clearMustChangePassword: () => void;
}

// Only restore if we have an actual token
const hasValidSession = saved?.accessToken && saved.accessToken.split(".").length === 3;

export const useAuthStore = create<AuthStore>((set, get) => ({
  isAuthenticated: !!hasValidSession,
  email: hasValidSession ? saved.email : null,
  accessToken: hasValidSession ? saved.accessToken : null,
  refreshToken: hasValidSession ? saved.refreshToken : null,
  requiresTotp: false,
  mustChangePassword: false,
  cookieMode: false,

  login: async (email, password, totpCode) => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, totp_code: totpCode || null }),
        credentials: "include",
      });

      const data = await res.json();

      if (!res.ok) {
        return { ok: false, error: data.detail || "Login failed" };
      }

      if (data.requires_totp) {
        set({ requiresTotp: true });
        return { ok: false, requiresTotp: true };
      }

      const authState = {
        isAuthenticated: true,
        email: data.email,
        accessToken: data.access_token,
        refreshToken: data.refresh_token,
        requiresTotp: false,
        mustChangePassword: data.must_change_password ?? false,
        cookieMode: data.cookie_mode ?? false,
      };

      set(authState);

      // Only store tokens in sessionStorage if NOT in cookie mode
      if (!data.cookie_mode) {
        saveSession({
          email: data.email,
          accessToken: data.access_token,
          refreshToken: data.refresh_token,
        });
      } else {
        // Cookie mode: only store email for UI (no tokens)
        saveSession({ email: data.email, accessToken: "", refreshToken: "" });
      }

      return { ok: true };
    } catch {
      return { ok: false, error: "Cannot connect to server" };
    }
  },

  logout: () => {
    const rt = get().refreshToken;
    fetch(`${BACKEND_URL}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt || "" }),
      credentials: "include",
    }).catch(() => {});

    clearSession();
    set({
      isAuthenticated: false,
      email: null,
      accessToken: null,
      refreshToken: null,
      requiresTotp: false,
      mustChangePassword: false,
      cookieMode: false,
    });
  },

  refresh: (() => {
    // Singleton: prevent concurrent refresh calls
    let pending: Promise<boolean> | null = null;
    return async () => {
      if (pending) return pending;
      pending = (async () => {
    const { refreshToken } = get();
    if (!refreshToken) return false;

    try {
      const res = await fetch(`${BACKEND_URL}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        credentials: "include",
      });
      if (!res.ok) return false;
      const data = await res.json();
      set({
        accessToken: data.access_token,
        refreshToken: data.refresh_token ?? get().refreshToken,
      });
      const session = loadSession();
      if (session) {
        session.accessToken = data.access_token;
        if (data.refresh_token) session.refreshToken = data.refresh_token;
        saveSession(session);
      }
      return true;
    } catch {
      return false;
    }
      })();
      pending.finally(() => { pending = null; });
      return pending;
    };
  })(),

  clearMustChangePassword: () => {
    set({ mustChangePassword: false });
  },
}));
