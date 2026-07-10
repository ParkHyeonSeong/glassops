import { describe, expect, it, vi } from "vitest";
import { persistSetting } from "../settingsStore";

describe("persistSetting", () => {
  it("returns false instead of throwing when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });

    expect(persistSetting("wallpaper", "ocean")).toBe(false);
  });
});
