import { create } from "zustand";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

interface AuthStore {
  isAuthenticated: boolean;
  email: string | null;
  accessToken: string | null;
  refreshToken: string | null;
  requiresTotp: boolean;

  login: (email: string, password: string, totpCode?: string) => Promise<{ ok: boolean; requiresTotp?: boolean; error?: string }>;
  logout: () => void;
  refresh: () => Promise<boolean>;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  isAuthenticated: false,
  email: null,
  accessToken: null,
  refreshToken: null,
  requiresTotp: false,

  login: async (email, password, totpCode) => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, totp_code: totpCode || null }),
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
        email: data.email,
        accessToken: data.access_token,
        refreshToken: data.refresh_token,
        requiresTotp: false,
      });
      return { ok: true };
    } catch {
      return { ok: false, error: "Cannot connect to server" };
    }
  },

  logout: () => {
    set({
      isAuthenticated: false,
      email: null,
      accessToken: null,
      refreshToken: null,
      requiresTotp: false,
    });
  },

  refresh: async () => {
    const { refreshToken } = get();
    if (!refreshToken) return false;

    try {
      const res = await fetch(`${BACKEND_URL}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      set({ accessToken: data.access_token });
      return true;
    } catch {
      return false;
    }
  },
}));
