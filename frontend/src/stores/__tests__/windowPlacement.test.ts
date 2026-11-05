import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "../authStore";
import { DOCK_DEFAULTS, useSettingsStore } from "../settingsStore";
import { useWindowStore } from "../windowStore";

function setViewport(width: number, height: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
}
const openHuge = () => {
  useWindowStore.setState({ lastSizes: { terminal: { width: 5000, height: 5000 } } });
  useWindowStore.getState().openWindow("terminal");
  return useWindowStore.getState().windows[0];
};
const snap = (side: "left" | "right") => {
  useWindowStore.getState().openWindow("terminal");
  const id = useWindowStore.getState().windows[0].id;
  useWindowStore.getState().snapWindow(id, side);
  return useWindowStore.getState().windows[0];
};

describe("window placement around the dock", () => {
  beforeEach(() => {
    setViewport(1200, 800);
    useAuthStore.setState({ role: "admin" });
    useWindowStore.setState({ windows: [], nextZIndex: 1, lastSizes: {} });
    useSettingsStore.setState({ dock: DOCK_DEFAULTS });
  });

  it("keeps a new window above a bottom dock", () => {
    expect(openHuge()).toMatchObject({ width: 1200, height: 800 - 36 - 80 });
  });

  it("keeps a new window right of a left dock", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position: "left" } });
    const w = openHuge();
    expect(w).toMatchObject({ width: 1200 - 80, height: 800 - 36 });
    expect(w.x).toBeGreaterThanOrEqual(80);
  });

  it("snaps halves into the space beside a right dock", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position: "right" } });
    expect(snap("right")).toMatchObject({ x: 560, y: 0, width: 560, height: 800 - 36 });
  });

  it("snaps below a top dock", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position: "top" } });
    expect(snap("left")).toMatchObject({ x: 0, y: 80, width: 600, height: 800 - 36 - 80 });
  });

  it("reserves nothing when auto-hide is on", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, autoHide: true } });
    expect(snap("left")).toMatchObject({ x: 0, y: 0, width: 600, height: 800 - 36 });
  });

  it("lets a new window take all the room below the menubar when auto-hide is on", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, autoHide: true } });
    expect(openHuge()).toMatchObject({ width: 1200, height: 800 - 36 });
  });

  // Sides the cases above leave out. They cannot pin the cascade start: `x >= 80` holds with
  // no reserve at all, because a new window never starts left of 100.
  it.each([
    ["right", { width: 1200 - 80, height: 800 - 36 }],
    ["top", { width: 1200, height: 800 - 36 - 80 }],
  ] as const)("clamps a new window's size to the room a %s dock leaves", (position, size) => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position } });
    expect(openHuge()).toMatchObject(size);
  });

  it.each([
    ["left", 80, 0],
    ["top", 0, 80],
  ] as const)("starts the cascade after a %s dock", (position, left, top) => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position } });
    useWindowStore.getState().openWindow("terminal");
    const { x, y } = useWindowStore.getState().windows[0];
    // Both axes share one cascade offset, so only the dock's share differs between them.
    const offset = x - left - 100;
    expect(offset).toBeGreaterThanOrEqual(0);
    expect(y).toBe(top + 50 + offset);
  });

  it("snaps both halves beside a left dock", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position: "left" } });
    expect(snap("left")).toMatchObject({ x: 80, y: 0, width: 560, height: 800 - 36 });
    expect(snap("right")).toMatchObject({ x: 640, y: 0, width: 560, height: 800 - 36 });
  });

  it("snaps above a bottom dock", () => {
    expect(snap("right")).toMatchObject({ x: 600, y: 0, width: 600, height: 800 - 36 - 80 });
  });
});
