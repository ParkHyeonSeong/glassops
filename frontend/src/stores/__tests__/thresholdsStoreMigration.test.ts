import { beforeEach, describe, expect, it, vi } from "vitest";

const LEGACY_KEY = "glassops_thresholds";
const CURRENT_KEY = "glassops-thresholds";

describe("thresholdsStore module-init migration", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetModules();   // the migration runs once, at module evaluation
  });

  it("moves legacy values into the live store and persists them", async () => {
    localStorage.setItem(LEGACY_KEY, JSON.stringify({
      cpuWarn: 61, cpuCrit: 86, memWarn: 66, memCrit: 89, diskCrit: 91,
    }));

    const { useThresholdsStore } = await import("../thresholdsStore");

    expect(useThresholdsStore.getState().thresholds.cpu).toEqual({ warn: 61, crit: 86 });
    // The zustand persist envelope must actually have been written.
    const persisted = JSON.parse(localStorage.getItem(CURRENT_KEY)!);
    expect(persisted.state.thresholds.cpu).toEqual({ warn: 61, crit: 86 });
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it("lets an existing current-key configuration win over the legacy one", async () => {
    localStorage.setItem(CURRENT_KEY, JSON.stringify({
      state: { thresholds: { cpu: { warn: 33, crit: 44 } }, muted: {} }, version: 0,
    }));
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ cpuWarn: 61, cpuCrit: 86 }));

    const { useThresholdsStore } = await import("../thresholdsStore");

    expect(useThresholdsStore.getState().thresholds.cpu).toEqual({ warn: 33, crit: 44 });
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
  });
});
