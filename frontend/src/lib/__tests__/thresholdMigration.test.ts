import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MetricKey, Threshold } from "../thresholds";
import { DEFAULT_THRESHOLDS } from "../thresholds";
import {
  CURRENT_KEY, LEGACY_KEY, migrateLegacyThresholds,
} from "../thresholdMigration";

describe("migrateLegacyThresholds", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("maps legacy glassops_thresholds into the current store when it is absent", () => {
    localStorage.setItem(LEGACY_KEY, JSON.stringify({
      cpuWarn: 60, cpuCrit: 85, memWarn: 65, memCrit: 88, diskCrit: 92,
    }));
    const applied: Partial<Record<MetricKey, Threshold>>[] = [];

    migrateLegacyThresholds((patch) => applied.push(patch));

    expect(applied).toHaveLength(1);
    expect(applied[0].cpu).toEqual({ warn: 60, crit: 85 });
    expect(applied[0].mem).toEqual({ warn: 65, crit: 88 });
    expect(applied[0].core).toBeUndefined();
  });

  it("does not clobber an existing glassops-thresholds value", () => {
    localStorage.setItem(CURRENT_KEY, JSON.stringify({ state: { thresholds: {} } }));
    localStorage.setItem(LEGACY_KEY, JSON.stringify({
      cpuWarn: 60, cpuCrit: 85, memWarn: 65, memCrit: 88, diskCrit: 92,
    }));
    const apply = vi.fn();

    migrateLegacyThresholds(apply);

    expect(apply).not.toHaveBeenCalled();
  });

  it("retires the legacy key whether or not it was migrated", () => {
    localStorage.setItem(CURRENT_KEY, JSON.stringify({ state: { thresholds: {} } }));
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ cpuWarn: 60, cpuCrit: 85 }));

    migrateLegacyThresholds(vi.fn());

    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it("keeps disk warn below a low migrated diskCrit", () => {
    // DEFAULT disk.warn is 75; a legacy diskCrit of 50 must not invert the pair.
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ diskCrit: 50 }));
    const applied: Partial<Record<MetricKey, Threshold>>[] = [];

    migrateLegacyThresholds((patch) => applied.push(patch));

    expect(DEFAULT_THRESHOLDS.disk.warn).toBe(75);
    expect(applied[0].disk).toEqual({ warn: 50, crit: 50 });
  });

  it("clamps migrated values into 0–100", () => {
    // The legacy store clamped on write, but a hand-edited or corrupted
    // localStorage value would otherwise flow straight into severityFor.
    localStorage.setItem(LEGACY_KEY, JSON.stringify({
      cpuWarn: -20, cpuCrit: 400, memWarn: 101, memCrit: 102, diskCrit: -5,
    }));
    const applied: Partial<Record<MetricKey, Threshold>>[] = [];

    migrateLegacyThresholds((patch) => applied.push(patch));

    expect(applied[0].cpu).toEqual({ warn: 0, crit: 100 });
    expect(applied[0].mem).toEqual({ warn: 100, crit: 100 });
    expect(applied[0].disk).toEqual({ warn: 0, crit: 0 });
  });

  it("ignores malformed legacy JSON without throwing", () => {
    localStorage.setItem(LEGACY_KEY, "{not json");
    const apply = vi.fn();

    expect(() => migrateLegacyThresholds(apply)).not.toThrow();
    expect(apply).not.toHaveBeenCalled();
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
  });
});
