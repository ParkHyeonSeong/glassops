import { create } from "zustand";

export const WALLPAPERS = [
  { id: "default", label: "Deep Space", css: "linear-gradient(135deg, #1a1a2e, #16213e)" },
  { id: "ocean", label: "Ocean", css: "linear-gradient(135deg, #0f2027, #203a43, #2c5364)" },
  { id: "sunset", label: "Sunset", css: "linear-gradient(135deg, #2d1b69, #6b2fa0, #c4256a)" },
  { id: "forest", label: "Forest", css: "linear-gradient(135deg, #0a1a0f, #1b3a2d, #2d5a3f)" },
  { id: "midnight", label: "Midnight", css: "linear-gradient(135deg, #0c0c1d, #1a1a3a)" },
];

type ThresholdKey = "cpuWarn" | "cpuCrit" | "memWarn" | "memCrit" | "diskCrit";

interface SettingsStore {
  wallpaper: string;
  alertThresholds: Record<ThresholdKey, number>;
  setWallpaper: (id: string) => void;
  setThreshold: (key: ThresholdKey, value: number) => void;
}

function loadSetting<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`glassops_${key}`);
    return v ? JSON.parse(v) : fallback;
  } catch { return fallback; }
}

function saveSetting(key: string, value: unknown) {
  try { localStorage.setItem(`glassops_${key}`, JSON.stringify(value)); } catch {}
}

const defaultThresholds = { cpuWarn: 70, cpuCrit: 90, memWarn: 80, memCrit: 90, diskCrit: 95 };

export const useSettingsStore = create<SettingsStore>((set) => ({
  wallpaper: loadSetting("wallpaper", "default"),
  alertThresholds: loadSetting("thresholds", defaultThresholds),

  setWallpaper: (id) => {
    set({ wallpaper: id });
    saveSetting("wallpaper", id);
  },

  setThreshold: (key, value) => {
    const VALID_KEYS: ThresholdKey[] = ["cpuWarn", "cpuCrit", "memWarn", "memCrit", "diskCrit"];
    if (!VALID_KEYS.includes(key)) return;
    set((state) => {
      const next = { ...state.alertThresholds, [key]: Math.max(1, Math.min(100, value)) };
      // Enforce warn <= crit
      if (key === "cpuWarn" && next.cpuWarn > next.cpuCrit) next.cpuCrit = next.cpuWarn;
      if (key === "cpuCrit" && next.cpuCrit < next.cpuWarn) next.cpuWarn = next.cpuCrit;
      if (key === "memWarn" && next.memWarn > next.memCrit) next.memCrit = next.memWarn;
      if (key === "memCrit" && next.memCrit < next.memWarn) next.memWarn = next.memCrit;
      saveSetting("thresholds", next);
      return { alertThresholds: next };
    });
  },
}));
