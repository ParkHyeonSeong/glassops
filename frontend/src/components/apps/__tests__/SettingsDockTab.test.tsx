import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { Profiler } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../stores/authStore";
import { useMetricsStore } from "../../../stores/metricsStore";
import {
  DOCK_DEFAULTS, DOCK_RANGES, useSettingsStore, type DockSettings,
} from "../../../stores/settingsStore";
import SettingsApp from "../Settings";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

function openDockTab() {
  render(<SettingsApp />);
  fireEvent.click(screen.getByRole("button", { name: "Dock" }));
}
const dock = () => useSettingsStore.getState().dock;

describe("Settings > Dock", () => {
  beforeEach(() => {
    useSettingsStore.setState({ dock: DOCK_DEFAULTS });
    useAuthStore.setState({ email: "admin@example.com", role: "admin" });
    useMetricsStore.setState({ agentId: null, connected: false });
  });

  it("groups the controls under Appearance, Size, Behavior and Show", () => {
    openDockTab();
    for (const heading of ["Appearance", "Size", "Behavior", "Show"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
  });
  it("updates selects live", () => {
    openDockTab();
    fireEvent.change(screen.getByRole("combobox", { name: "Position" }), { target: { value: "left" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Theme" }), { target: { value: "light" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Spring" }), { target: { value: "bouncy" } });
    expect(dock()).toMatchObject({ position: "left", theme: "light", spring: "bouncy" });
  });
  it("updates sliders live and shows the value with its unit", () => {
    openDockTab();
    fireEvent.change(screen.getByRole("slider", { name: "Item size" }), { target: { value: "60" } });
    fireEvent.change(screen.getByRole("slider", { name: "Roundness" }), { target: { value: "0.25" } });
    expect(dock()).toMatchObject({ baseItemSize: 60, roundness: 0.25 });
    expect(screen.getByText("60px")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Magnification range" })).toHaveAttribute("max", "400");
  });
  it("updates colours live", () => {
    openDockTab();
    fireEvent.change(screen.getByLabelText("Accent color"), { target: { value: "#ff0000" } });
    expect(dock().accentColor).toBe("#ff0000");
  });
  it("toggles switches", () => {
    openDockTab();
    const labels = screen.getByRole("switch", { name: "Labels" });
    expect(labels).toHaveAttribute("aria-checked", "true");
    fireEvent.click(labels);
    expect(dock().showLabels).toBe(false);
    expect(labels).toHaveAttribute("aria-checked", "false");
  });
  it("resets to defaults", () => {
    useSettingsStore.getState().setDock({ position: "left", tiles: false });
    openDockTab();
    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    expect(dock()).toEqual(DOCK_DEFAULTS);
  });
});

// The Dock tab control by control (spec 4 and 13): the tab's place, the layout, the options, the
// value each control shows, the one key each writes, and Reset. The tests start from a dock that is
// mostly non-default, with distinct numbers and neighbouring switches alternating, so a control
// that shows, writes or resets the wrong key cannot pass by coincidence.
const STORED: DockSettings = {
  theme: "light",
  accentColor: "#112233",
  badgeColor: "#445566",
  position: "right",
  baseItemSize: 40,
  magnification: 90,
  distance: 100,
  panelHeight: 80,
  gap: 4,
  roundness: 0.2,
  spring: "gentle",
  bounce: false,
  autoHide: true,
  tiles: true,
  showLabels: false,
  showIndicators: true,
  showBadges: false,
};

type TextKey = "theme" | "position" | "spring" | "accentColor" | "badgeColor";
type FlagKey = "tiles" | "bounce" | "autoHide" | "showLabels" | "showIndicators" | "showBadges";

const SELECTS: { name: string; key: TextKey; to: string }[] = [
  { name: "Theme", key: "theme", to: "dark" },
  { name: "Position", key: "position", to: "top" },
  { name: "Spring", key: "spring", to: "snappy" },
];
const COLORS: { name: string; key: TextKey; to: string }[] = [
  { name: "Accent color", key: "accentColor", to: "#ff0000" },
  { name: "Badge color", key: "badgeColor", to: "#00ff00" },
];
const SLIDERS: { name: string; key: keyof typeof DOCK_RANGES; unit: string; to: number }[] = [
  { name: "Roundness", key: "roundness", unit: "", to: 0.75 },
  { name: "Item size", key: "baseItemSize", unit: "px", to: 64 },
  { name: "Magnification", key: "magnification", unit: "px", to: 120 },
  { name: "Magnification range", key: "distance", unit: "px", to: 350 },
  { name: "Bar height", key: "panelHeight", unit: "px", to: 96 },
  { name: "Gap", key: "gap", unit: "px", to: 22 },
];
const SWITCHES: { name: string; key: FlagKey }[] = [
  { name: "Tiles", key: "tiles" },
  { name: "Bounce", key: "bounce" },
  { name: "Auto-hide", key: "autoHide" },
  { name: "Labels", key: "showLabels" },
  { name: "Indicators", key: "showIndicators" },
  { name: "Badges", key: "showBadges" },
];

// The store holds `expected`, and so does localStorage: the tab goes through setDock.
function expectSaved(expected: DockSettings) {
  expect(dock()).toEqual(expected);
  expect(JSON.parse(localStorage.getItem("glassops_dock") ?? "null")).toEqual(expected);
}

function expectControlsToShow(d: DockSettings) {
  for (const { name, key } of SELECTS) expect(screen.getByRole("combobox", { name })).toHaveValue(d[key]);
  for (const { name, key } of COLORS) {
    const input = screen.getByLabelText(name);
    expect(input).toHaveAttribute("type", "color");
    expect(input).toHaveValue(d[key]);
  }
  for (const { name, key, unit } of SLIDERS) {
    expect(screen.getByRole("slider", { name })).toHaveValue(String(d[key]));
    expect(screen.getByText(`${d[key]}${unit}`)).toBeInTheDocument();
  }
  for (const { name, key } of SWITCHES) {
    const toggle = screen.getByRole("switch", { name });
    expect(toggle).toHaveAttribute("aria-checked", String(d[key]));
    expect(toggle.classList.contains("settings-toggle-on")).toBe(d[key]);
  }
}

describe("Settings > Dock controls", () => {
  beforeEach(() => {
    localStorage.removeItem("glassops_dock");
    useSettingsStore.setState({ dock: STORED, wallpaper: "default" });
    useAuthStore.setState({ email: "admin@example.com", role: "admin" });
    useMetricsStore.setState({ agentId: null, connected: false });
  });

  it("lists Dock right after Appearance in the sidebar", () => {
    render(<SettingsApp />);
    const names = screen.getAllByRole("button").map((button) => button.textContent);
    expect(names.indexOf("Dock")).toBe(names.indexOf("Appearance") + 1);
  });
  it("shows the Dock controls only on the Dock tab", () => {
    render(<SettingsApp />);
    expect(screen.queryByRole("slider", { name: "Item size" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dock" }));
    expect(screen.getByRole("slider", { name: "Item size" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Appearance" }));
    expect(screen.queryByRole("slider", { name: "Item size" })).toBeNull();
  });
  it("lays the controls out in the documented groups and order", () => {
    openDockTab();
    const layout = [
      "Appearance", "Theme", "Accent color", "Badge color", "Roundness", "Tiles",
      "Size", "Item size", "Magnification", "Magnification range", "Bar height", "Gap",
      "Behavior", "Position", "Spring", "Bounce", "Auto-hide",
      "Show", "Labels", "Indicators", "Badges",
      "Reset to defaults",
    ];
    const find = (name: string) =>
      screen.queryByRole("heading", { name })
      ?? screen.queryByLabelText(name)
      ?? screen.getByRole("button", { name });
    const elements = layout.map(find);
    for (let i = 1; i < layout.length; i++) {
      const follows = elements[i - 1]!.compareDocumentPosition(elements[i]!) & Node.DOCUMENT_POSITION_FOLLOWING;
      expect(follows, `${layout[i]} comes after ${layout[i - 1]}`).toBeTruthy();
    }
  });
  it("offers the documented options in each select", () => {
    openDockTab();
    const options = (name: string) =>
      within(screen.getByRole("combobox", { name })).getAllByRole("option")
        .map((option) => [(option as HTMLOptionElement).value, option.textContent]);
    expect(options("Theme")).toEqual([["dark", "Dark"], ["light", "Light"]]);
    expect(options("Position")).toEqual([
      ["bottom", "Bottom"], ["top", "Top"], ["left", "Left"], ["right", "Right"],
    ]);
    expect(options("Spring")).toEqual([
      ["smooth", "Smooth"], ["snappy", "Snappy"], ["bouncy", "Bouncy"], ["gentle", "Gentle"],
    ]);
  });
  it("shows the stored settings in every control", () => {
    openDockTab();
    expectControlsToShow(STORED);
  });
  it("brings every control back to the defaults on reset", () => {
    openDockTab();
    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    expectControlsToShow(DOCK_DEFAULTS);
    expectSaved(DOCK_DEFAULTS);
  });
  it.each(SELECTS)("$name select writes only $key", ({ name, key, to }) => {
    openDockTab();
    fireEvent.change(screen.getByRole("combobox", { name }), { target: { value: to } });
    expectSaved({ ...STORED, [key]: to });
  });
  it.each(COLORS)("$name input writes only $key", ({ name, key, to }) => {
    openDockTab();
    fireEvent.change(screen.getByLabelText(name), { target: { value: to } });
    expectSaved({ ...STORED, [key]: to });
  });
  it.each(SLIDERS)("$name slider spans its range and writes only $key", ({ name, key, unit, to }) => {
    openDockTab();
    const slider = screen.getByRole("slider", { name });
    const { min, max, step } = DOCK_RANGES[key];
    expect(slider).toHaveAttribute("min", String(min));
    expect(slider).toHaveAttribute("max", String(max));
    expect(slider).toHaveAttribute("step", String(step));
    fireEvent.change(slider, { target: { value: String(to) } });
    expectSaved({ ...STORED, [key]: to });
    expect(screen.getByText(`${to}${unit}`)).toBeInTheDocument();
  });
  it.each(SWITCHES)("$name switch is a button that flips only $key", ({ name, key }) => {
    openDockTab();
    const toggle = screen.getByRole("switch", { name });
    // A real button, so Space and Enter toggle it without extra key handling.
    expect(toggle.tagName).toBe("BUTTON");
    fireEvent.click(toggle);
    expectSaved({ ...STORED, [key]: !STORED[key] });
    expect(toggle).toHaveAttribute("aria-checked", String(!STORED[key]));
    expect(toggle.classList.contains("settings-toggle-on")).toBe(!STORED[key]);
    fireEvent.click(toggle);
    expectSaved(STORED);
    expect(toggle).toHaveAttribute("aria-checked", String(STORED[key]));
  });
  it("re-renders for dock changes only, not for other settings", () => {
    const onRender = vi.fn();
    render(
      <Profiler id="settings" onRender={onRender}>
        <SettingsApp />
      </Profiler>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dock" }));
    onRender.mockClear();
    act(() => useSettingsStore.setState({ wallpaper: "ocean" }));
    expect(onRender).not.toHaveBeenCalled();
    act(() => useSettingsStore.getState().setDock({ gap: 12 }));
    expect(onRender).toHaveBeenCalled();
  });
});
