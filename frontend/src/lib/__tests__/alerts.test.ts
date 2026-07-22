import { describe, expect, it } from "vitest";
import { makeMetricSnapshot } from "../../test/fixtures";
import { DEFAULT_THRESHOLDS } from "../thresholds";
import { deriveAlerts } from "../alerts";

// 32 pegged cores — the exact shape that used to produce 32 extra alerts.
const PEGGED = Array.from({ length: 32 }, () => 100);

describe("deriveAlerts", () => {
  it("emits no alert when total CPU is normal even if every core is pegged", () => {
    const snap = makeMetricSnapshot({
      cpu: { percent_total: 40, percent_per_core: PEGGED, count_logical: 32,
             count_physical: 16, freq_current: 1000, freq_max: 2000 },
    });

    expect(deriveAlerts(snap, DEFAULT_THRESHOLDS)).toEqual([]);
  });

  it("emits exactly one CPU alert when total CPU is critical and every core is high", () => {
    const snap = makeMetricSnapshot({
      cpu: { percent_total: 99, percent_per_core: PEGGED, count_logical: 32,
             count_physical: 16, freq_current: 1000, freq_max: 2000 },
    });

    const alerts = deriveAlerts(snap, DEFAULT_THRESHOLDS);

    expect(alerts).toHaveLength(1);
    expect(alerts[0].id).toBe("cpu");
    expect(alerts[0].metric).toBe("cpu");
    expect(alerts[0].severity).toBe("crit");
  });

  it("never emits an alert scoped to an individual core", () => {
    const snap = makeMetricSnapshot({
      cpu: { percent_total: 99, percent_per_core: PEGGED, count_logical: 32,
             count_physical: 16, freq_current: 1000, freq_max: 2000 },
      memory: { total: 1024, available: 0, used: 1024, percent: 99,
                swap_total: 0, swap_used: 0, swap_percent: 0 },
      disk: { total: 1024, used: 1024, free: 0, percent: 99,
              read_bytes: 0, write_bytes: 0 },
    });

    for (const alert of deriveAlerts(snap, DEFAULT_THRESHOLDS)) {
      expect(alert.id).not.toMatch(/^core:/);
    }
  });

  it("still emits memory warn and crit alerts", () => {
    const warn = makeMetricSnapshot({
      memory: { total: 1024, available: 128, used: 896, percent: 90,
                swap_total: 0, swap_used: 0, swap_percent: 0 },
    });
    const crit = makeMetricSnapshot({
      memory: { total: 1024, available: 8, used: 1016, percent: 97,
                swap_total: 0, swap_used: 0, swap_percent: 0 },
    });

    expect(deriveAlerts(warn, DEFAULT_THRESHOLDS).map((a) => [a.id, a.severity]))
      .toEqual([["mem", "warn"]]);
    expect(deriveAlerts(crit, DEFAULT_THRESHOLDS).map((a) => [a.id, a.severity]))
      .toEqual([["mem", "crit"]]);
  });

  it("still emits disk warn and crit alerts", () => {
    const warn = makeMetricSnapshot({
      disk: { total: 1024, used: 800, free: 224, percent: 80,
              read_bytes: 0, write_bytes: 0 },
    });
    const crit = makeMetricSnapshot({
      disk: { total: 1024, used: 960, free: 64, percent: 94,
              read_bytes: 0, write_bytes: 0 },
    });

    expect(deriveAlerts(warn, DEFAULT_THRESHOLDS).map((a) => [a.id, a.severity]))
      .toEqual([["disk", "warn"]]);
    expect(deriveAlerts(crit, DEFAULT_THRESHOLDS).map((a) => [a.id, a.severity]))
      .toEqual([["disk", "crit"]]);
  });

  it("fires at the exact threshold value (>= contract)", () => {
    const snap = makeMetricSnapshot({
      cpu: { percent_total: DEFAULT_THRESHOLDS.cpu.warn, percent_per_core: [1],
             count_logical: 1, count_physical: 1, freq_current: 1000, freq_max: 2000 },
    });

    const alerts = deriveAlerts(snap, DEFAULT_THRESHOLDS);

    expect(alerts).toHaveLength(1);
    expect(alerts[0].severity).toBe("warn");
    expect(alerts[0].threshold).toBe(DEFAULT_THRESHOLDS.cpu.warn);
  });

  it("sorts critical before warning", () => {
    const snap = makeMetricSnapshot({
      cpu: { percent_total: 82, percent_per_core: [82], count_logical: 1,
             count_physical: 1, freq_current: 1000, freq_max: 2000 },
      disk: { total: 1024, used: 1000, free: 24, percent: 97,
              read_bytes: 0, write_bytes: 0 },
    });

    expect(deriveAlerts(snap, DEFAULT_THRESHOLDS).map((a) => a.id)).toEqual(["disk", "cpu"]);
  });
});
