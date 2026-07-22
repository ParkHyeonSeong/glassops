import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { type MetricKey, type Threshold, DEFAULT_THRESHOLDS, clampThreshold } from "../lib/thresholds";
import { migrateLegacyThresholds } from "../lib/thresholdMigration";

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
      // Deep-merge persisted thresholds over the defaults so a missing/added
      // MetricKey (schema change or corrupted storage) never yields an undefined
      // threshold, which would crash severityFor (t.crit on undefined). merge runs
      // on every rehydrate regardless of version, so no version bump is needed.
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<ThresholdsStore>;
        return {
          ...current,
          ...p,
          thresholds: { ...DEFAULT_THRESHOLDS, ...(p.thresholds ?? {}) },
          muted: p.muted ?? {},
        };
      },
    },
  ),
);

// Retire the settingsStore-era `glassops_thresholds` key on first load. setState
// here goes through zustand's persist middleware, so the migrated values are
// written to `glassops-thresholds` immediately.
migrateLegacyThresholds((patch) =>
  useThresholdsStore.setState((s) => ({ thresholds: { ...s.thresholds, ...patch } })),
);
