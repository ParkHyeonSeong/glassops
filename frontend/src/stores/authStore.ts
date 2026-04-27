import { create } from "zustand";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";
const SESSION_KEY = "glassops_auth";

type StoredSession = { email: string; accessToken: string; refreshToken: string };

function loadSession(): StoredSession | null {
  try {
    const data = sessionStorage.getItem(SESSION_KEY);
    if (data) return JSON.parse(data);
  } catch { /* ignore */ }
  return null;
}

function saveSession(state: StoredSession) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(state));
  } catch { /* ignore */ }
}

function clearSession() {
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch { /* ignore */ }
}

// Cross-tab sync: login/logout on one tab propagates to others via BroadcastChannel.
// Same-origin, browser-native — no server connection needed.
const authChannel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("glassops_auth") : null;

const saved = loadSession();
const hasTokenSession = !!(saved?.accessToken && saved.accessToken.split(".").length === 3);
const hasCookieSession = !!(saved?.email && !saved.accessToken);
const hasValidSession = hasTokenSession || hasCookieSession;

interface AuthStore {
  isAuthenticated: boolean;
  isBootstrapping: boolean;
  email: string | null;
  role: "admin" | "user" | null;
  hostAccounts: Record<string, string>;
  accessToken: string | null;
  refreshToken: string | null;
  requiresTotp: boolean;
  mustChangePassword: boolean;
  cookieMode: boolean;

  bootstrap: () => Promise<void>;
  login: (email: string, password: string, totpCode?: string) => Promise<{ ok: boolean; requiresTotp?: boolean; error?: string }>;
  logout: () => void;
  refresh: () => Promise<boolean>;
  clearMustChangePassword: () => void;
}

const loggedOutState = {
  isAuthenticated: false,
  email: null,
  role: null as "admin" | "user" | null,
  hostAccounts: {} as Record<string, string>,
  accessToken: null,
  refreshToken: null,
  requiresTotp: false,
  mustChangePassword: false,
  cookieMode: false,
};

export const useAuthStore = create<AuthStore>((set, get) => ({
  // Optimistic restore from sessionStorage — bootstrap() will verify with server.
  isAuthenticated: hasValidSession,
  // Skip the loading gate when we already have an optimistic session; otherwise gate
  // rendering so a fresh tab with shared cookies doesn't flash the login screen.
  isBootstrapping: !hasValidSession,
  email: hasValidSession ? saved!.email : null,
  role: null,
  hostAccounts: {},
  accessToken: hasTokenSession ? saved!.accessToken : null,
  refreshToken: hasTokenSession ? saved!.refreshToken : null,
  requiresTotp: false,
  mustChangePassword: false,
  cookieMode: hasCookieSession,

  bootstrap: async () => {
    const fetchMe = async () => {
      const headers: Record<string, string> = {};
      const token = get().accessToken;
      if (token) headers["Authorization"] = `Bearer ${token}`;
      return fetch(`${BACKEND_URL}/api/auth/me`, { headers, credentials: "include" });
    };

    const applyMe = (data: {
      email: string;
      must_change_password?: boolean;
      role?: "admin" | "user";
      host_accounts?: Record<string, string>;
    }) => {
      set({
        isAuthenticated: true,
        email: data.email,
        role: data.role ?? "user",
        hostAccounts: data.host_accounts ?? {},
        mustChangePassword: data.must_change_password ?? false,
        // If we succeeded without a bearer token, auth came via httpOnly cookie.
        cookieMode: get().cookieMode || !get().accessToken,
        isBootstrapping: false,
      });
      saveSession({
        email: data.email,
        accessToken: get().accessToken ?? "",
        refreshToken: get().refreshToken ?? "",
      });
    };

    try {
      let res = await fetchMe();

      if (res.status === 401) {
        // Access token missing/expired — try refresh (works via cookie even without state).
        // Temporarily mark cookieMode so refresh() doesn't early-return on a fresh tab.
        if (!get().cookieMode && !get().refreshToken) set({ cookieMode: true });
        const refreshed = await get().refresh();
        if (refreshed) res = await fetchMe();
      }

      if (res.ok) {
        applyMe(await res.json());
      } else {
        clearSession();
        set({ ...loggedOutState, isBootstrapping: false });
      }
    } catch {
      // Network failure — don't wipe an optimistic session, just stop gating render.
      set({ isBootstrapping: false });
    }
  },

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

      set({
        isAuthenticated: true,
        isBootstrapping: false,
        email: data.email,
        role: data.role ?? "user",
        hostAccounts: data.host_accounts ?? {},
        accessToken: data.access_token,
        refreshToken: data.refresh_token,
        requiresTotp: false,
        mustChangePassword: data.must_change_password ?? false,
        cookieMode: data.cookie_mode ?? false,
      });

      if (!data.cookie_mode) {
        saveSession({
          email: data.email,
          accessToken: data.access_token,
          refreshToken: data.refresh_token,
        });
      } else {
        saveSession({ email: data.email, accessToken: "", refreshToken: "" });
      }

      authChannel?.postMessage({ type: "login" });
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
    set({ ...loggedOutState, isBootstrapping: false });
    authChannel?.postMessage({ type: "logout" });
  },

  refresh: (() => {
    let pending: Promise<boolean> | null = null;
    return async () => {
      if (pending) return pending;
      pending = (async () => {
        const { refreshToken, cookieMode } = get();
        if (!refreshToken && !cookieMode) return false;

        try {
          const res = await fetch(`${BACKEND_URL}/api/auth/refresh`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: refreshToken || "" }),
            credentials: "include",
          });
          if (!res.ok) return false;
          const data = await res.json();
          // In cookie mode, the server already re-set httpOnly cookies; don't persist tokens locally.
          if (!get().cookieMode) {
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

// Handle cross-tab messages. Apply state directly (no broadcast) to avoid loops.
authChannel?.addEventListener("message", (e) => {
  const type = (e.data as { type?: string } | null)?.type;
  if (type === "logout") {
    clearSession();
    useAuthStore.setState({ ...loggedOutState, isBootstrapping: false });
  } else if (type === "login") {
    // Another tab logged in — pull identity via /auth/me using shared cookies.
    useAuthStore.getState().bootstrap();
  }
});
