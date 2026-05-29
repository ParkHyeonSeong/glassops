import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { type MetricKey, type Threshold, DEFAULT_THRESHOLDS, clampThreshold } from "../lib/thresholds";

interface ThresholdsStore {
  thresholds: Record<MetricKey, Threshold>;
  muted: Record<string, number>; // alertId -> expiry (epoch ms)
  setThreshold: (key: MetricKey, t: Threshold) => void;
  reset: () => void;
  mute: (alertId: string, untilMs: number) => void;
}

export const useThresholdsStore = create<ThresholdsStore>()(
  persist(
    (set) => ({
      thresholds: DEFAULT_THRESHOLDS,
      muted: {},
      setThreshold: (key, t) =>
        set((s) => ({ thresholds: { ...s.thresholds, [key]: clampThreshold(t) } })),
      reset: () => set({ thresholds: DEFAULT_THRESHOLDS, muted: {} }),
      mute: (alertId, untilMs) =>
        set((s) => ({ muted: { ...s.muted, [alertId]: untilMs } })),
    }),
    {
      name: "glassops-thresholds", // localStorage key
      storage: createJSONStorage(() => localStorage),
    },
  ),
);
