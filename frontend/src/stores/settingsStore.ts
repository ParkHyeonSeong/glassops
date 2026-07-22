import { create } from "zustand";

export const WALLPAPERS = [
  { id: "default", label: "Deep Space", css: "linear-gradient(135deg, #1a1a2e, #16213e)" },
  { id: "ocean", label: "Ocean", css: "linear-gradient(135deg, #0f2027, #203a43, #2c5364)" },
  { id: "sunset", label: "Sunset", css: "linear-gradient(135deg, #2d1b69, #6b2fa0, #c4256a)" },
  { id: "forest", label: "Forest", css: "linear-gradient(135deg, #0a1a0f, #1b3a2d, #2d5a3f)" },
  { id: "midnight", label: "Midnight", css: "linear-gradient(135deg, #0c0c1d, #1a1a3a)" },
];

interface SettingsStore {
  wallpaper: string;
  setWallpaper: (id: string) => void;
}

function loadSetting<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`glassops_${key}`);
    return v ? JSON.parse(v) : fallback;
  } catch { return fallback; }
}

export function persistSetting(key: string, value: unknown): boolean {
  try {
    localStorage.setItem(`glassops_${key}`, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

// Alert thresholds used to live here under `glassops_thresholds`, in parallel with
// (and disagreeing with) useThresholdsStore. They now live only in useThresholdsStore;
// see lib/thresholdMigration.ts for the one-shot migration off the old key.
export const useSettingsStore = create<SettingsStore>((set) => ({
  wallpaper: loadSetting("wallpaper", "default"),

  setWallpaper: (id) => {
    set({ wallpaper: id });
    persistSetting("wallpaper", id);
  },
}));
