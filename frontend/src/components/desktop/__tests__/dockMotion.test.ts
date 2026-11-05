import { describe, expect, it } from "vitest";
import { DOCK_DEFAULTS, DOCK_SPRINGS } from "../../../stores/settingsStore";
import {
  DOCK_EDGE, dockMetrics, dockReserve, fitScale, isSettled, magnifiedSize, peakSwell, placeMenu, startHop, stepHop,
  stepSpring,
} from "../dockMotion";

describe("dockMetrics", () => {
  it("derives peak, thickness and pad from the settings", () => {
    expect(dockMetrics(DOCK_DEFAULTS, 1)).toEqual({ base: 50, peak: 70, thickness: 68, pad: 9, gap: 10, distance: 200 });
  });
  it("never lets peak or thickness fall below the item size", () => {
    expect(dockMetrics({ ...DOCK_DEFAULTS, baseItemSize: 60, magnification: 40, panelHeight: 30 }, 1))
      .toMatchObject({ base: 60, peak: 60, thickness: 60, pad: 0 });
  });
  it("scales every length by the fit factor", () => {
    expect(dockMetrics(DOCK_DEFAULTS, 0.5)).toEqual({ base: 25, peak: 35, thickness: 34, pad: 4.5, gap: 5, distance: 100 });
  });
});

describe("magnifiedSize", () => {
  it("is the peak under the pointer and the base at or beyond the range", () => {
    expect(magnifiedSize(0, 50, 70, 200)).toBe(70);
    expect(magnifiedSize(200, 50, 70, 200)).toBe(50);
    expect(magnifiedSize(-350, 50, 70, 200)).toBe(50);
  });
  it("follows the raised-cosine falloff", () => {
    expect(magnifiedSize(100, 50, 70, 200)).toBeCloseTo(60, 6);
    expect(magnifiedSize(60, 50, 70, 200)).toBeCloseTo(65.8779, 3);
  });
  it("is symmetric and non-increasing with distance", () => {
    let previous = Infinity;
    for (let d = 0; d <= 220; d += 10) {
      const size = magnifiedSize(d, 50, 70, 200);
      expect(size).toBeLessThanOrEqual(previous);
      expect(magnifiedSize(-d, 50, 70, 200)).toBe(size);
      previous = size;
    }
  });
});

describe("stepSpring", () => {
  it.each(Object.entries(DOCK_SPRINGS))("%s settles on the target within 180 frames at 60 fps", (_name, spring) => {
    let state = { size: 50, velocity: 0 };
    let frames = 0;
    while (!isSettled(state, 70) && frames < 180) { state = stepSpring(state, 70, 1 / 60, spring); frames += 1; }
    expect(isSettled(state, 70)).toBe(true);
  });
  it("stays stable and still settles with long frames", () => {
    let state = { size: 50, velocity: 0 };
    for (let i = 0; i < 40; i += 1) {
      state = stepSpring(state, 70, 0.25, DOCK_SPRINGS.snappy);
      expect(Number.isFinite(state.size)).toBe(true);
      expect(Math.abs(state.size - 70)).toBeLessThanOrEqual(20);
    }
    expect(isSettled(state, 70)).toBe(true);
  });
  it("treats tiny error and velocity as settled", () => {
    expect(isSettled({ size: 69.995, velocity: 0.005 }, 70)).toBe(true);
    expect(isSettled({ size: 69.9, velocity: 0 }, 70)).toBe(false);
  });
});

describe("fitScale", () => {
  it("is 1 when the dock fits and shrinks proportionally otherwise", () => {
    expect(fitScale(500, 800)).toBe(1);
    expect(fitScale(800, 400)).toBe(0.5);
  });
  it("never goes below 0.4 and treats an empty dock as fitting", () => {
    expect(fitScale(1000, 100)).toBe(0.4);
    expect(fitScale(0, 100)).toBe(1);
  });
});

describe("peakSwell", () => {
  it("is one item's swell when no two items are in range of the pointer together", () => {
    expect(peakSwell([100], 50, 70, 200)).toBe(20);
    expect(peakSwell([0, 400], 50, 70, 200)).toBe(20);
  });
  it("adds up the items that swell together, with the pointer where that sum is largest", () => {
    // Two items 100px apart: 30 with the pointer on one, 2 × 10 × (1 + cos(π/4)) halfway between.
    expect(peakSwell([0, 100], 50, 70, 200)).toBeCloseTo(20 * (1 + Math.SQRT1_2), 6);
    // The default admin dock at rest: 9 items 60px apart, 11px more across the separator. Most
    // halfway between two items (66.81); on an item it is at most 66.55.
    expect(peakSwell([34, 94, 154, 214, 274, 334, 394, 465, 525], 50, 70, 200)).toBeCloseTo(66.807, 2);
  });
  it("is 0 without magnification or without items", () => {
    expect(peakSwell([0, 60, 120], 50, 50, 200)).toBe(0);
    expect(peakSwell([], 50, 70, 200)).toBe(0);
  });
});

describe("hop", () => {
  it("peaks at 0.4 × size halfway through the 0.38 s airtime", () => {
    let hop = startHop(50);
    let highest = 0;
    for (let t = 0; t < 0.19; t += 0.001) { hop = stepHop(hop, 0.001); highest = Math.max(highest, hop.lift); }
    expect(highest).toBeCloseTo(20, 0);
  });
  it("lands, rebounds lower and stops", () => {
    let hop = startHop(50);
    let t = 0;
    while (hop.active && t < 2) { hop = stepHop(hop, 1 / 240); t += 1 / 240; }
    expect(hop).toMatchObject({ active: false, lift: 0 });
    // One rebound: the 0.38 s arc, then 0.36 × 0.38 s, so it lands for good at about 0.517 s.
    expect(t).toBeGreaterThan(0.5);
    expect(t).toBeLessThan(0.54);
  });
});

describe("dockReserve", () => {
  it("reserves EDGE + bar thickness on the dock's side", () => {
    expect(DOCK_EDGE).toBe(12);
    expect(dockReserve(DOCK_DEFAULTS)).toEqual({ top: 0, right: 0, bottom: 80, left: 0 });
    expect(dockReserve({ ...DOCK_DEFAULTS, position: "left" })).toEqual({ top: 0, right: 0, bottom: 0, left: 80 });
    expect(dockReserve({ ...DOCK_DEFAULTS, position: "top" })).toEqual({ top: 80, right: 0, bottom: 0, left: 0 });
    expect(dockReserve({ ...DOCK_DEFAULTS, position: "right", panelHeight: 30 })).toEqual({ top: 0, right: 62, bottom: 0, left: 0 });
  });
  it("reserves nothing with auto-hide", () => {
    expect(dockReserve({ ...DOCK_DEFAULTS, autoHide: true })).toEqual({ top: 0, right: 0, bottom: 0, left: 0 });
  });
});

describe("placeMenu", () => {
  const viewport = { width: 1000, height: 800 };
  const anchor = { left: 475, top: 700, right: 525, bottom: 750 };
  it("puts the menu above a bottom dock with the tail on the item", () => {
    expect(placeMenu(anchor, { width: 200, height: 120 }, "bottom", viewport))
      .toEqual({ x: 400, y: 563, tail: 100, maxHeight: 675 });
  });
  it("keeps the menu 8px inside the viewport and the tail clear of the corner", () => {
    const p = placeMenu({ left: 0, top: 700, right: 50, bottom: 750 }, { width: 200, height: 120 }, "bottom", viewport);
    expect(p.x).toBe(8);
    expect(p.tail).toBe(19);
  });
  it("opens beside left and right docks", () => {
    expect(placeMenu({ left: 12, top: 375, right: 62, bottom: 425 }, { width: 180, height: 100 }, "left", viewport))
      .toEqual({ x: 79, y: 350, tail: 50, maxHeight: 784 });
    expect(placeMenu({ left: 938, top: 375, right: 988, bottom: 425 }, { width: 180, height: 100 }, "right", viewport).x)
      .toBe(741);
  });
  it("keeps a side dock's menu 8px inside the viewport vertically, its tail on the item but clear of the corner", () => {
    expect(placeMenu({ left: 12, top: 740, right: 62, bottom: 790 }, { width: 180, height: 200 }, "left", viewport))
      .toEqual({ x: 79, y: 592, tail: 173, maxHeight: 784 });
    expect(placeMenu({ left: 938, top: 0, right: 988, bottom: 50 }, { width: 180, height: 200 }, "right", viewport))
      .toEqual({ x: 741, y: 8, tail: 19, maxHeight: 784 });
  });
  it("caps the height so a long menu scrolls inside the viewport", () => {
    expect(placeMenu(anchor, { width: 200, height: 2000 }, "bottom", viewport)).toMatchObject({ y: 8, maxHeight: 675 });
  });
});
