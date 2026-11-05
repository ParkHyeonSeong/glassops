import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DOCK_DEFAULTS, DOCK_RANGES, DOCK_SPRINGS, sanitizeDockSettings, useSettingsStore,
} from "../settingsStore";

describe("dock settings constants", () => {
  it("exposes the spec defaults", () => {
    expect(DOCK_DEFAULTS).toEqual({
      theme: "dark", accentColor: "#0a84ff", badgeColor: "#ff453a", position: "bottom",
      baseItemSize: 50, magnification: 70, distance: 200, panelHeight: 68, gap: 10, roundness: 0.5,
      spring: "smooth", bounce: true, autoHide: false, tiles: true,
      showLabels: true, showIndicators: true, showBadges: true,
    });
  });

  it("exposes the spec ranges and spring presets", () => {
    expect(DOCK_RANGES).toEqual({
      baseItemSize: { min: 30, max: 80, step: 2 }, magnification: { min: 30, max: 140, step: 2 },
      distance: { min: 60, max: 400, step: 10 }, panelHeight: { min: 30, max: 120, step: 2 },
      gap: { min: 0, max: 30, step: 1 }, roundness: { min: 0, max: 1, step: 0.05 },
    });
    expect(DOCK_SPRINGS).toEqual({
      smooth: { mass: 0.1, stiffness: 150, damping: 12 }, snappy: { mass: 0.1, stiffness: 420, damping: 20 },
      bouncy: { mass: 0.1, stiffness: 220, damping: 4 }, gentle: { mass: 0.2, stiffness: 120, damping: 14 },
    });
  });
});

describe("sanitizeDockSettings", () => {
  it("returns the defaults for anything that is not a plain object", () => {
    for (const raw of [null, undefined, "x", 42, [], true]) {
      expect(sanitizeDockSettings(raw)).toEqual(DOCK_DEFAULTS);
    }
  });

  it("keeps valid values", () => {
    const valid = { ...DOCK_DEFAULTS, theme: "light", position: "left", baseItemSize: 60,
      spring: "bouncy", autoHide: true, accentColor: "#123abc" };
    expect(sanitizeDockSettings(valid)).toEqual(valid);
  });

  it("replaces invalid enums and colours with defaults", () => {
    const out = sanitizeDockSettings({ theme: "neon", position: "middle", spring: "wobbly",
      accentColor: "red", badgeColor: "#12345" });
    expect(out).toMatchObject({ theme: "dark", position: "bottom", spring: "smooth",
      accentColor: "#0a84ff", badgeColor: "#ff453a" });
  });

  it("takes #rrggbb colours in either case, as given, and nothing longer", () => {
    expect(sanitizeDockSettings({ accentColor: "#ABCDEF", badgeColor: "#1234567" }))
      .toMatchObject({ accentColor: "#ABCDEF", badgeColor: "#ff453a" });
  });

  it("clamps numbers into range and ignores wrong types", () => {
    const out = sanitizeDockSettings({ baseItemSize: 500, magnification: -3, distance: "300",
      roundness: 1.7, gap: Number.NaN, tiles: "yes" });
    expect(out).toMatchObject({ baseItemSize: 80, magnification: 30, distance: 200,
      roundness: 1, gap: 10, tiles: true });
  });

  it("drops unknown keys", () => {
    expect(sanitizeDockSettings({ ...DOCK_DEFAULTS, extra: 1 })).not.toHaveProperty("extra");
  });
});

describe("dock settings store", () => {
  beforeEach(() => useSettingsStore.setState({ dock: DOCK_DEFAULTS }));
  afterEach(() => localStorage.removeItem("glassops_dock"));

  it("setDock merges, sanitises and persists under glassops_dock", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    useSettingsStore.getState().setDock({ position: "left", baseItemSize: 999 });
    const dock = useSettingsStore.getState().dock;
    expect(dock).toEqual({ ...DOCK_DEFAULTS, position: "left", baseItemSize: 80 });
    expect(setItem).toHaveBeenCalledWith("glassops_dock", JSON.stringify(dock));
  });

  it("resetDock restores and persists the defaults", () => {
    useSettingsStore.getState().setDock({ tiles: false, theme: "light" });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    useSettingsStore.getState().resetDock();
    expect(useSettingsStore.getState().dock).toEqual(DOCK_DEFAULTS);
    expect(setItem).toHaveBeenCalledWith("glassops_dock", JSON.stringify(DOCK_DEFAULTS));
  });

  it("still updates in memory when storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });
    expect(() => useSettingsStore.getState().setDock({ gap: 4 })).not.toThrow();
    expect(useSettingsStore.getState().dock.gap).toBe(4);
  });

  it("loads and sanitises the stored value at startup", async () => {
    localStorage.setItem("glassops_dock", JSON.stringify({ position: "top", baseItemSize: 1000, theme: 5 }));
    vi.resetModules();
    const fresh = await import("../settingsStore");
    expect(fresh.useSettingsStore.getState().dock).toEqual({ ...fresh.DOCK_DEFAULTS, position: "top", baseItemSize: 80 });
  });

  it("falls back to defaults when the stored value is not JSON", async () => {
    localStorage.setItem("glassops_dock", "{oops");
    vi.resetModules();
    const fresh = await import("../settingsStore");
    expect(fresh.useSettingsStore.getState().dock).toEqual(fresh.DOCK_DEFAULTS);
  });
});
