import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveAlerts } from "../../../hooks/useActiveAlerts";
import { useKeyboardShortcuts } from "../../../hooks/useKeyboardShortcuts";
import { useAuthStore } from "../../../stores/authStore";
import { DOCK_DEFAULTS, useSettingsStore } from "../../../stores/settingsStore";
import { useWindowStore } from "../../../stores/windowStore";
import type { WindowState } from "../../../types";
import Dock from "../Dock";

vi.mock("../../../hooks/useActiveAlerts", () => ({ useActiveAlerts: vi.fn(() => []) }));

let seq = 0;
function win(appId: string, extra: Partial<WindowState> = {}): WindowState {
  seq += 1;
  return { id: `w${seq}`, appId, title: appId, x: 0, y: 0, width: 400, height: 300, minWidth: 100,
    minHeight: 100, isMinimized: false, isMaximized: false, zIndex: seq, opacity: 1, preMaximizeBounds: null, ...extra };
}
const bar = () => screen.getByRole("toolbar", { name: "Dock" });
const button = (name: string) => within(bar()).getByRole("button", { name });

describe("Dock", () => {
  beforeEach(() => {
    useAuthStore.setState({ role: "admin" });
    useWindowStore.setState({ windows: [], nextZIndex: 1, lastSizes: {} });
    useSettingsStore.setState({ dock: DOCK_DEFAULTS });
    vi.mocked(useActiveAlerts).mockReturnValue([]);
  });
  afterEach(() => vi.useRealTimers());

  it("renders the launcher apps with one separator before Settings and one tab stop", () => {
    render(<Dock />);
    const buttons = within(bar()).getAllByRole("button");
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual(["System Monitor", "GPU Monitor", "Docker",
      "Network", "Process Viewer", "Logs", "Terminal", "Settings", "Users"]);
    expect(within(bar()).getAllByRole("separator")).toHaveLength(1);
    expect(buttons.filter((b) => b.tabIndex === 0)).toHaveLength(1);
  });

  it("names open apps, shows their dot and the System Monitor alert badge", () => {
    vi.mocked(useActiveAlerts).mockReturnValue([{}, {}] as ReturnType<typeof useActiveAlerts>);
    useWindowStore.setState({ windows: [win("terminal"), win("container-logs")] });
    const { container } = render(<Dock />);
    expect(button("Terminal (open)")).toHaveAttribute("data-open");
    expect(button("Docker (open)")).toHaveAttribute("data-open");
    expect(container.querySelectorAll(".dock-pip")).toHaveLength(2);
    expect(button("System Monitor").querySelector(".dock-count")).toHaveTextContent("2");
  });

  it("opens an app on click", () => {
    render(<Dock />);
    fireEvent.click(button("Network"));
    expect(useWindowStore.getState().windows.map((w) => w.appId)).toEqual(["network"]);
  });

  it("opens the app menu on right-click and quits the whole group", () => {
    useWindowStore.setState({ windows: [win("docker", { title: "Docker" }), win("container-logs", { title: "Logs: nginx" })] });
    render(<Dock />);
    fireEvent.contextMenu(button("Docker (open)"));
    const menu = screen.getByRole("menu");
    expect(Array.from(menu.querySelectorAll('[role^="menuitem"]'), (r) => r.textContent)).toEqual(
      ["Docker", "Logs: nginx", "Show All Windows", "Hide", "Quit"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Quit" }));
    expect(useWindowStore.getState().windows).toEqual([]);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens the menu from the keyboard and returns focus on Escape", () => {
    render(<Dock />);
    const monitor = button("System Monitor");
    monitor.focus();
    fireEvent.keyDown(monitor, { key: "ContextMenu" });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Open" }), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(monitor).toHaveFocus();
    fireEvent.keyDown(monitor, { key: "ArrowUp" });
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("moves focus along the bar with the arrow keys, Home and End", () => {
    render(<Dock />);
    const first = button("System Monitor");
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(button("GPU Monitor")).toHaveFocus();
    fireEvent.keyDown(button("GPU Monitor"), { key: "End" });
    expect(button("Users")).toHaveFocus();
    fireEvent.keyDown(button("Users"), { key: "ArrowRight" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "ArrowLeft" });
    expect(button("Users")).toHaveFocus();
    expect(button("Users").tabIndex).toBe(0);
  });

  it("opens the menu on a touch long press without also launching the app", () => {
    vi.useFakeTimers();
    render(<Dock />);
    fireEvent.pointerDown(button("Network"), { pointerType: "touch", clientX: 10, clientY: 10 });
    act(() => { vi.advanceTimersByTime(450); });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.pointerUp(button("Network"), { pointerType: "touch" });
    fireEvent.click(button("Network"));
    expect(useWindowStore.getState().windows).toEqual([]);
  });

  it("Escape in the menu leaves the focused window open", () => {
    function Shortcuts() { useKeyboardShortcuts(); return null; }
    useWindowStore.setState({ windows: [win("terminal", { title: "Terminal" })], nextZIndex: 10 });
    render(<><Shortcuts /><Dock /></>);
    fireEvent.contextMenu(button("Terminal (open)"));
    fireEvent.keyDown(screen.getAllByRole("menuitemcheckbox")[0], { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(useWindowStore.getState().windows).toHaveLength(1);
  });

  it("keeps focus in the menu when Ctrl+W takes its row away, so Escape closes only the menu", () => {
    function Shortcuts() { useKeyboardShortcuts(); return null; }
    useWindowStore.setState({ windows: [win("settings", { title: "Settings" }), win("logs", { title: "Logs" })],
      nextZIndex: 100 }); // Logs in front
    render(<><Shortcuts /><Dock /></>);
    const press = (init: object) => fireEvent.keyDown(document.activeElement!, init);
    const titles = () => useWindowStore.getState().windows.map((w) => w.title);
    act(() => button("Logs (open)").focus());
    press({ key: "ArrowUp" });
    press({ key: "End" });
    expect(screen.getByRole("menuitem", { name: "Quit" })).toHaveFocus();
    press({ key: "w", ctrlKey: true }); // closes the front window, Logs, and its menu loses Quit
    expect(titles()).toEqual(["Settings"]);
    expect(screen.getByRole("menuitem", { name: "Open" })).toHaveFocus();
    press({ key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(titles()).toEqual(["Settings"]);
    expect(button("Logs")).toHaveFocus();
  });

  it("honours the display, theme and position settings", () => {
    useWindowStore.setState({ windows: [win("terminal")] });
    vi.mocked(useActiveAlerts).mockReturnValue([{}] as ReturnType<typeof useActiveAlerts>);
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, showLabels: false, showIndicators: false, showBadges: false,
      tiles: false, theme: "light", position: "left" } });
    const { container } = render(<Dock />);
    expect(container.querySelector(".dock-tip, .dock-pip, .dock-count")).toBeNull();
    expect(bar()).toHaveAttribute("data-bare");
    expect(bar()).toHaveAttribute("data-theme", "light");
    expect(bar()).toHaveAttribute("data-position", "left");
    expect(bar()).toHaveAttribute("aria-orientation", "vertical");
  });

  it("hands its sizes and colors to the CSS and keeps the glass layer apart from the items", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, baseItemSize: 40, panelHeight: 60, gap: 6, roundness: 1,
      accentColor: "#123456", badgeColor: "#abcdef" } });
    render(<Dock />);
    const props = ["--dock-base", "--dock-thickness", "--dock-pad", "--dock-gap", "--dock-radius", "--dock-roundness",
      "--dock-edge", "--dock-accent", "--dock-badge"];
    expect(props.map((p) => bar().style.getPropertyValue(p))).toEqual(
      ["40px", "60px", "10px", "6px", "30px", "1", "12px", "#123456", "#abcdef"]);
    for (const item of within(bar()).getAllByRole("button")) {
      expect([item.style.width, item.style.height]).toEqual(["40px", "40px"]);
    }
    // A tile or label inside the glass layer would lose its own backdrop blur.
    const glass = bar().querySelector(".dock-glass");
    expect(glass?.parentElement).toBe(bar());
    expect(glass?.childElementCount).toBe(0);
  });

  it("labels every app and badges only System Monitor", () => {
    vi.mocked(useActiveAlerts).mockReturnValue([{}] as ReturnType<typeof useActiveAlerts>);
    const { container } = render(<Dock />);
    expect(Array.from(container.querySelectorAll(".dock-tip"), (tip) => tip.textContent)).toEqual(["System Monitor",
      "GPU Monitor", "Docker", "Network", "Process Viewer", "Logs", "Terminal", "Settings", "Users"]);
    expect(container.querySelectorAll(".dock-count")).toHaveLength(1);
  });

  it("leaves admin-only apps out for other roles and keeps a tab stop when its item goes", () => {
    render(<Dock />);
    button("Users").focus();
    act(() => useAuthStore.setState({ role: "user" }));
    expect(within(bar()).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual(["System Monitor",
      "GPU Monitor", "Network", "Process Viewer", "Logs", "Terminal", "Settings"]);
    expect(button("System Monitor").tabIndex).toBe(0);
  });

  it.each([
    ["bottom", "ArrowLeft", "ArrowRight", "ArrowUp", "vertical"],
    ["top", "ArrowLeft", "ArrowRight", "ArrowDown", "vertical"],
    ["left", "ArrowUp", "ArrowDown", "ArrowRight", "horizontal"],
    ["right", "ArrowUp", "ArrowDown", "ArrowLeft", "horizontal"],
  ] as const)("on the %s edge moves with %s and %s and opens the menu with %s", (position, prev, next, away, rule) => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position } });
    render(<Dock />);
    expect(within(bar()).getByRole("separator")).toHaveAttribute("aria-orientation", rule);
    const first = button("System Monitor");
    first.focus();
    expect(fireEvent.keyDown(first, { key: next })).toBe(false); // handled, so the page does not scroll
    expect(button("GPU Monitor")).toHaveFocus();
    fireEvent.keyDown(button("GPU Monitor"), { key: prev });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: away });
    expect(screen.getByRole("menu").closest(".dock-menu")).toHaveAttribute("data-side", position);
    expect(screen.getByRole("menuitem", { name: "Open" })).toHaveFocus();
  });

  it("opens the menu with Shift+F10 and leaves keys with Alt, Ctrl or Meta alone", () => {
    render(<Dock />);
    const logs = button("Logs");
    logs.focus();
    expect(fireEvent.keyDown(logs, { key: "ArrowRight", altKey: true })).toBe(true);
    expect(logs).toHaveFocus();
    fireEvent.keyDown(logs, { key: "Home" });
    const monitor = button("System Monitor");
    expect(monitor).toHaveFocus();
    fireEvent.keyDown(monitor, { key: "F10" });
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.keyDown(monitor, { key: "F10", shiftKey: true });
    expect(screen.getByRole("menuitem", { name: "Open" })).toHaveFocus();
  });

  it("marks the item while its menu is open and refocuses it only after Escape or Tab", () => {
    render(<Dock />);
    const terminal = button("Terminal");
    terminal.focus();
    fireEvent.keyDown(terminal, { key: "ArrowUp" });
    expect(within(bar()).getAllByRole("button").filter((b) => b.hasAttribute("data-menu-open"))).toEqual([terminal]);
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Open" }), { key: "Tab" });
    expect(terminal).toHaveFocus();
    expect(terminal).not.toHaveAttribute("data-menu-open");
    // From the pointer the list itself takes focus, with no row lit; a pick leaves focus alone.
    expect(fireEvent.contextMenu(terminal)).toBe(false);
    expect(screen.getByRole("menu")).toHaveFocus();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    expect(useWindowStore.getState().windows.map((w) => w.appId)).toEqual(["terminal"]);
    expect(button("Terminal (open)")).not.toHaveFocus();
  });

  it("anchors the menu on its item in the dock's theme and accent", () => {
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, theme: "light", accentColor: "#123456" } });
    render(<Dock />);
    const logs = button("Logs");
    vi.spyOn(logs, "getBoundingClientRect").mockReturnValue(
      { x: 500, y: 700, left: 500, top: 700, right: 550, bottom: 750, width: 50, height: 50, toJSON() {} } as DOMRect);
    fireEvent.contextMenu(logs);
    const root = document.querySelector<HTMLElement>("[data-dock-menu]");
    expect(root).toHaveAttribute("data-theme", "light");
    expect(root?.style.getPropertyValue("--dock-menu-accent")).toBe("#123456");
    // jsdom lays the menu out 0px wide, so centred on the item it starts at the item's centre.
    expect(root?.querySelector<HTMLElement>(".dock-menu")?.style.left).toBe("525px");
  });

  it("runs Hide, Show All Windows and the window rows on the window store", () => {
    useWindowStore.setState({ windows: [win("docker", { title: "Docker" }), win("container-logs", { title: "Logs: nginx" })],
      nextZIndex: 100 });
    render(<Dock />);
    const pick = (role: "menuitem" | "menuitemcheckbox", name: string) => {
      fireEvent.contextMenu(button("Docker (open)"));
      fireEvent.click(within(screen.getByRole("menu")).getByRole(role, { name }));
    };
    const state = () => useWindowStore.getState().windows.map((w) => [w.title, w.isMinimized]);
    pick("menuitem", "Hide");
    expect(state()).toEqual([["Docker", true], ["Logs: nginx", true]]);
    pick("menuitem", "Show All Windows");
    expect(state()).toEqual([["Docker", false], ["Logs: nginx", false]]);
    pick("menuitemcheckbox", "Docker");
    const [docker, logs] = useWindowStore.getState().windows;
    expect(docker.zIndex).toBeGreaterThan(logs.zIndex);
    act(() => useWindowStore.getState().minimizeWindow(logs.id));
    pick("menuitemcheckbox", "Logs: nginx");
    expect(state()).toEqual([["Docker", false], ["Logs: nginx", false]]);
  });

  it("keeps the open menu in step with the windows", () => {
    render(<Dock />);
    fireEvent.contextMenu(button("Terminal"));
    act(() => useWindowStore.setState({ windows: [win("terminal", { title: "Terminal" })] }));
    expect(Array.from(screen.getByRole("menu").querySelectorAll('[role^="menuitem"]'), (r) => r.textContent)).toEqual(
      ["Terminal", "Show All Windows", "Hide", "Quit"]);
  });

  it("takes only a touch or pen held still for 450 ms as a long press", () => {
    vi.useFakeTimers();
    render(<Dock />);
    const network = button("Network");
    fireEvent.pointerDown(network, { pointerId: 1, pointerType: "mouse", clientX: 10, clientY: 10 });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.pointerDown(network, { pointerId: 1, pointerType: "pen", clientX: 10, clientY: 10 });
    fireEvent.pointerMove(network, { pointerId: 1, pointerType: "pen", clientX: 14, clientY: 14 }); // under 6px away
    fireEvent.pointerMove(button("Logs"), { pointerId: 2, pointerType: "touch", clientX: 300, clientY: 300 });
    act(() => { vi.advanceTimersByTime(449); });
    expect(screen.queryByRole("menu")).toBeNull();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole("menu")).toHaveFocus();
  });

  it("drops a long press on lift, cancel, drift, a new press or unmount", () => {
    vi.useFakeTimers();
    const { unmount } = render(<Dock />);
    const network = button("Network");
    const press = () => fireEvent.pointerDown(network, { pointerId: 1, pointerType: "touch", clientX: 10, clientY: 10 });
    for (const end of [
      () => fireEvent.pointerUp(network, { pointerId: 1, pointerType: "touch" }),
      () => fireEvent.pointerCancel(network, { pointerId: 1, pointerType: "touch" }),
      () => fireEvent.pointerMove(network, { pointerId: 1, pointerType: "touch", clientX: 10, clientY: 17 }),
    ]) {
      press();
      act(() => { vi.advanceTimersByTime(300); });
      end();
      act(() => { vi.advanceTimersByTime(1000); });
      expect(screen.queryByRole("menu")).toBeNull();
    }
    press();
    act(() => { vi.advanceTimersByTime(300); });
    fireEvent.pointerDown(button("Logs"), { pointerId: 2, pointerType: "touch", clientX: 50, clientY: 10 });
    act(() => { vi.advanceTimersByTime(300); });
    expect(screen.queryByRole("menu")).toBeNull();
    act(() => { vi.advanceTimersByTime(150); });
    expect(button("Logs")).toHaveAttribute("data-menu-open");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    // Counted from here: jsdom queues timers of its own whenever focus moves.
    const pending = vi.getTimerCount();
    press();
    expect(vi.getTimerCount()).toBe(pending + 1);
    unmount();
    expect(vi.getTimerCount()).toBe(pending);
  });

  it("swallows only the click that ends a long press, also after the browser's own menu", () => {
    vi.useFakeTimers();
    render(<Dock />);
    const longPress = (name: string, ms: number) => {
      fireEvent.pointerDown(button(name), { pointerType: "touch" });
      act(() => { vi.advanceTimersByTime(ms); });
    };
    const apps = () => useWindowStore.getState().windows.map((w) => w.appId);
    // No click came (the browser dropped it): closing the menu forgets the press.
    longPress("Network", 450);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    fireEvent.click(button("Network"));
    expect(apps()).toEqual(["network"]);
    longPress("Logs", 450);
    fireEvent.pointerUp(button("Logs"), { pointerType: "touch" });
    fireEvent.click(button("Logs"));
    expect(apps()).toEqual(["network"]);
    fireEvent.click(button("Logs"));
    expect(apps()).toEqual(["network", "logs"]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    // The browser's long-press menu can come first; the press then ends there.
    longPress("Terminal", 400);
    fireEvent.contextMenu(button("Terminal"));
    act(() => { vi.advanceTimersByTime(100); });
    fireEvent.pointerUp(button("Terminal"), { pointerType: "touch" });
    fireEvent.click(button("Terminal"));
    expect(apps()).toEqual(["network", "logs"]);
  });
});
