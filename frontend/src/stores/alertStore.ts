import { create } from "zustand";

export interface Alert {
  id: string;
  type: "info" | "warning" | "error";
  message: string;
  createdAt: number;
}

const MAX_ALERTS = 5;
const AUTO_DISMISS_MS = 8000;

const _lastFired: Record<string, number> = {};
const _timers: Record<string, ReturnType<typeof setTimeout>> = {};
const COOLDOWN_MS = 30000;

interface AlertStore {
  alerts: Alert[];
  push: (type: Alert["type"], message: string, key?: string) => void;
  dismiss: (id: string) => void;
}

export const useAlertStore = create<AlertStore>((set) => ({
  alerts: [],

  push: (type, message, key) => {
    if (key) {
      const now = Date.now();
      if (_lastFired[key] && now - _lastFired[key] < COOLDOWN_MS) return;
      _lastFired[key] = now;
    }

    const id = `alert-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const alert: Alert = { id, type, message, createdAt: Date.now() };

    set((state) => ({
      alerts: [...state.alerts, alert].slice(-MAX_ALERTS),
    }));

    _timers[id] = setTimeout(() => {
      set((state) => ({ alerts: state.alerts.filter((a) => a.id !== id) }));
      delete _timers[id];
    }, AUTO_DISMISS_MS);

    if (type === "error" && "Notification" in globalThis && Notification.permission === "granted") {
      new Notification("GlassOps Alert", { body: message });
    }
  },

  dismiss: (id) => {
    if (_timers[id]) {
      clearTimeout(_timers[id]);
      delete _timers[id];
    }
    set((state) => ({ alerts: state.alerts.filter((a) => a.id !== id) }));
  },
}));
