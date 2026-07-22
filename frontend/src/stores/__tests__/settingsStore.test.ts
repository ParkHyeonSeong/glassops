import { describe, expect, it, vi } from "vitest";
import { persistSetting, useSettingsStore } from "../settingsStore";

describe("persistSetting", () => {
  it("returns false instead of throwing when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });

    expect(persistSetting("wallpaper", "ocean")).toBe(false);
  });

  it("updates in-memory settings when browser storage quota is exceeded", () => {
    const originalState = useSettingsStore.getState();
    const nextWallpaper = originalState.wallpaper === "ocean" ? "forest" : "ocean";

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });

    try {
      expect(() => useSettingsStore.getState().setWallpaper(nextWallpaper)).not.toThrow();
      expect(useSettingsStore.getState().wallpaper).toBe(nextWallpaper);
    } finally {
      useSettingsStore.setState({ wallpaper: originalState.wallpaper });
    }
  });
});
