import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../stores/authStore";
import { DOCK_DEFAULTS, useSettingsStore } from "../../../stores/settingsStore";
import { useWindowStore } from "../../../stores/windowStore";
import Dock from "../Dock";

vi.mock("../../../hooks/useActiveAlerts", () => ({ useActiveAlerts: () => [] }));

function setViewport(width: number, height: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
}
function motion(reduce: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: reduce && query.includes("reduce"), media: query,
    onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false }));
}
function frames() {
  let queue: FrameRequestCallback[] = [];
  let now = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { queue.push(cb); return queue.length; });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  return { run(ms: number) { const end = now + ms; while (now < end) { now += 16; const q = queue; queue = []; q.forEach((cb) => cb(now)); } } };
}
// Default admin dock: 9 apps + 1 separator → restLength = 9×50 + 1 + 9×10 + 2×9 = 559.
function placeBar(centreX: number) {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const r = this.getAttribute("role") === "toolbar"
      ? { x: centreX - 279.5, y: 720, width: 559, height: 68 } : { x: 0, y: 0, width: 0, height: 0 };
    return { ...r, left: r.x, top: r.y, right: r.x + r.width, bottom: r.y + r.height, toJSON() {} } as DOMRect;
  });
}
const bar = () => screen.getByRole("toolbar", { name: "Dock" });
const items = () => within(bar()).getAllByRole("button");
const width = (el: HTMLElement) => Number.parseFloat(el.style.width);

describe("Dock motion", () => {
  beforeEach(() => {
    setViewport(1200, 800);
    useAuthStore.setState({ role: "admin" });
    useWindowStore.setState({ windows: [], nextZIndex: 1, lastSizes: {} });
    useSettingsStore.setState({ dock: DOCK_DEFAULTS });
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

  it("magnifies around the pointer and relaxes when it leaves", () => {
    motion(true); placeBar(600);
    render(<Dock />);
    fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 }); // first item's rest centre
    expect(width(items()[0])).toBeCloseTo(70, 2);
    expect(width(items()[1])).toBeCloseTo(65.878, 2);
    expect(width(items()[8])).toBeCloseTo(50, 2);
    fireEvent.pointerLeave(bar());
    expect(items().map(width)).toEqual(Array(9).fill(50));
  });

  it("magnifies the keyboard-focused item", () => {
    motion(true); placeBar(600);
    render(<Dock />);
    items()[0].focus();
    fireEvent.keyDown(items()[0], { key: "ArrowRight" });
    expect(width(items()[1])).toBeCloseTo(70, 2);
  });

  it("springs toward the target over frames when motion is allowed", () => {
    motion(false); placeBar(600); const clock = frames();
    render(<Dock />);
    fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 });
    act(() => clock.run(48));
    expect(width(items()[0])).toBeGreaterThan(50);
    expect(width(items()[0])).toBeLessThan(70);
    act(() => clock.run(2000));
    expect(width(items()[0])).toBeCloseTo(70, 1);
  });

  it("hops on activation and lands", () => {
    motion(false); placeBar(600); const clock = frames();
    render(<Dock />);
    fireEvent.click(items()[3]);
    expect(items()[3]).toHaveAttribute("data-hop");
    act(() => clock.run(1500));
    expect(items()[3]).not.toHaveAttribute("data-hop");
  });

  it("does not hop with bounce off or reduced motion", () => {
    motion(true); placeBar(600);
    render(<Dock />);
    fireEvent.click(items()[3]);
    expect(items()[3]).not.toHaveAttribute("data-hop");
    act(() => useSettingsStore.getState().setDock({ bounce: false }));
    motion(false);
    fireEvent.click(items()[4]);
    expect(items()[4]).not.toHaveAttribute("data-hop");
  });

  it("shrinks to fit a narrow viewport", () => {
    motion(true); setViewport(400, 800);
    render(<Dock />);
    // 400 − 2 × 12 of room for the rest length plus the most the items grow together.
    expect(width(items()[0])).toBeCloseTo(50 * (376 / (559 + 66.81)), 1);
  });

  it("auto-hides and reveals near its edge", () => {
    motion(true); vi.useFakeTimers();
    useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, autoHide: true } });
    render(<Dock />);
    expect(bar()).toHaveAttribute("data-hidden");
    fireEvent.pointerMove(window, { clientX: 600, clientY: 790 });
    expect(bar()).not.toHaveAttribute("data-hidden");
    fireEvent.pointerMove(window, { clientX: 600, clientY: 300 });
    act(() => { vi.advanceTimersByTime(299); });
    expect(bar()).not.toHaveAttribute("data-hidden");
    act(() => { vi.advanceTimersByTime(10); });
    expect(bar()).toHaveAttribute("data-hidden");
  });

  it("hides while a window is maximised and stays revealed while focused or showing a menu", () => {
    motion(true); vi.useFakeTimers();
    useWindowStore.setState({ windows: [{ id: "m", appId: "terminal", title: "Terminal", x: 0, y: 0, width: 1, height: 1,
      minWidth: 1, minHeight: 1, isMinimized: false, isMaximized: true, zIndex: 1, opacity: 1, preMaximizeBounds: null }] });
    render(<Dock />);
    expect(bar()).toHaveAttribute("data-hidden");
    act(() => items()[0].focus());
    expect(bar()).not.toHaveAttribute("data-hidden");
    fireEvent.contextMenu(items()[0]);
    act(() => items()[0].blur());
    act(() => { vi.advanceTimersByTime(1000); });
    expect(bar()).not.toHaveAttribute("data-hidden");
  });

  describe("in detail", () => {
    // Animation frames under the test's control: at(t) runs the frames due at t ms, run(ms)
    // steps 16 ms frames, pending() counts the frames waiting to run.
    function frameClock() {
      let queue: FrameRequestCallback[] = [];
      let now = 0;
      vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { queue.push(cb); return queue.length; });
      vi.stubGlobal("cancelAnimationFrame", () => {});
      const at = (t: number) => { now = t; const q = queue; queue = []; q.forEach((cb) => cb(t)); };
      return { at, run(ms: number) { const end = now + ms; while (now < end) at(now + 16); }, pending: () => queue.length };
    }
    // placeBar for a side dock: the toolbar's centre along its height.
    function placeColumn(centreY: number) {
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
        const r = this.getAttribute("role") === "toolbar"
          ? { x: 12, y: centreY - 279.5, width: 68, height: 559 } : { x: 0, y: 0, width: 0, height: 0 };
        return { ...r, left: r.x, top: r.y, right: r.x + r.width, bottom: r.y + r.height, toJSON() {} } as DOMRect;
      });
    }
    const tile = (item: HTMLElement) => item.querySelector(".dock-tile") as HTMLElement;
    const autoHide = () => useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, autoHide: true } });

    it("lets the pointer lead while it is over the bar, then the focused item", () => {
      motion(true); placeBar(600);
      render(<Dock />);
      act(() => items()[8].focus());
      fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 });
      expect(width(items()[0])).toBeCloseTo(70, 2);
      expect(width(items()[8])).toBe(50);
      fireEvent.pointerLeave(bar());
      expect(width(items()[8])).toBeCloseTo(70, 2);
      expect(width(items()[0])).toBe(50);
    });

    it("magnifies focus only when the keyboard moved it, and relaxes when focus leaves", () => {
      motion(true); placeBar(600);
      render(<Dock />);
      fireEvent.pointerDown(items()[3], { pointerType: "mouse" });
      act(() => items()[3].focus()); // a browser focuses the button the mouse pressed
      expect(items().map(width)).toEqual(Array(9).fill(50));
      fireEvent.keyDown(items()[3], { key: "ArrowRight" });
      expect(width(items()[4])).toBeCloseTo(70, 2);
      act(() => items()[4].blur());
      expect(items().map(width)).toEqual(Array(9).fill(50));
    });

    it("does not take shortcut or modifier keys for keyboard navigation", () => {
      motion(true); placeBar(600); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      fireEvent.pointerDown(items()[3], { pointerType: "mouse" });
      act(() => items()[3].focus()); // a browser focuses the button the mouse pressed
      // A key, then the window loses focus and gets it back, as with Cmd+Tab away and back.
      const keyThenRefocus = (init: object) => {
        fireEvent.keyDown(items()[3], init);
        act(() => items()[3].blur());
        act(() => items()[3].focus());
        act(() => { vi.advanceTimersByTime(300); });
      };
      for (const init of [{ key: "Meta", metaKey: true }, { key: "Tab", metaKey: true }, { key: "c", ctrlKey: true },
        { key: "Tab", altKey: true }, { key: "Shift", shiftKey: true }, { key: "CapsLock" }, { key: "AltGraph" }]) {
        keyThenRefocus(init);
        expect(items().map(width)).toEqual(Array(9).fill(50));
        expect(bar()).toHaveAttribute("data-hidden");
      }
      keyThenRefocus({ key: "Tab", shiftKey: true }); // Shift+Tab is keyboard navigation
      expect(width(items()[3])).toBeCloseTo(70, 2);
      expect(bar()).not.toHaveAttribute("data-hidden");
    });

    it("does not stay shown for a focus the mouse put on an item", () => {
      motion(true); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      fireEvent.pointerMove(window, { clientX: 600, clientY: 790 }); // the edge strip reveals it
      fireEvent.pointerMove(bar(), { clientX: 600, clientY: 760 });
      fireEvent.pointerDown(items()[3], { pointerType: "mouse" });
      act(() => items()[3].focus()); // a browser focuses the button the mouse pressed
      fireEvent.click(items()[3]);
      fireEvent.pointerLeave(bar());
      fireEvent.pointerMove(window, { clientX: 600, clientY: 300 });
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
      expect(items()[3]).toHaveFocus();
    });

    it("lets the mouse take over from keyboard focus, also with a click on the focused item", () => {
      motion(true); placeBar(600); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      act(() => items()[0].focus()); // Tab into the dock
      fireEvent.keyDown(items()[0], { key: "ArrowRight" });
      expect(items()[1]).toHaveFocus(); // GPU Monitor
      expect(width(items()[1])).toBeCloseTo(70, 2);
      expect(bar()).not.toHaveAttribute("data-hidden");
      // The mouse clicks the item that already has focus, so no focus event follows, then leaves.
      fireEvent.pointerMove(bar(), { clientX: 414.5, clientY: 750 }); // GPU Monitor's rest centre
      fireEvent.pointerDown(items()[1], { pointerType: "mouse" });
      fireEvent.click(items()[1]);
      fireEvent.pointerLeave(bar());
      fireEvent.pointerMove(window, { clientX: 600, clientY: 300 });
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
      expect(items().map(width)).toEqual(Array(9).fill(50));
      expect(items()[1]).toHaveFocus();
    });

    it("ends the keyboard focus's swell and reveal on a press elsewhere that leaves focus alone", () => {
      motion(true); placeBar(600); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      act(() => items()[0].focus());
      fireEvent.keyDown(items()[0], { key: "ArrowRight" });
      expect(width(items()[1])).toBeCloseTo(70, 2);
      fireEvent.pointerDown(document.body, { pointerType: "mouse" });
      expect(items().map(width)).toEqual(Array(9).fill(50));
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
      expect(items()[1]).toHaveFocus();
    });

    it("holds the swell while a menu is open and lets go once it closes", () => {
      motion(true); placeBar(600);
      render(<Dock />);
      fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 });
      fireEvent.contextMenu(items()[0]);
      fireEvent.pointerLeave(bar()); // on its way into the menu, which is outside the toolbar
      expect(width(items()[0])).toBeCloseTo(70, 2);
      fireEvent.pointerDown(document.body); // a click elsewhere closes the menu
      expect(screen.queryByRole("menu")).toBeNull();
      expect(items().map(width)).toEqual(Array(9).fill(50));
    });

    it.each(["left", "right"] as const)("follows the pointer along a %s dock", (position) => {
      motion(true); placeColumn(418);
      useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position } });
      render(<Dock />);
      fireEvent.pointerMove(bar(), { clientX: 40, clientY: 172.5 }); // first item's rest centre
      expect(width(items()[0])).toBeCloseTo(70, 2);
      expect(width(items()[1])).toBeCloseTo(65.878, 2);
    });

    it("steps the spring by the time between frames, from the first frame on, at most 0.1 s a frame", () => {
      motion(false); placeBar(600); const clock = frameClock();
      render(<Dock />);
      fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 });
      act(() => clock.at(16));
      expect(width(items()[0])).toBeGreaterThan(50);
      act(() => clock.at(1016)); // a second later, say after a background tab
      expect(width(items()[0])).toBeGreaterThan(60);
      expect(width(items()[0])).toBeLessThan(69);
    });

    it("puts its sizes back after React rewrites them, then settles exactly and stops", () => {
      motion(false); placeBar(600); const clock = frameClock();
      render(<Dock />);
      fireEvent.pointerMove(bar(), { clientX: 354.5, clientY: 750 });
      act(() => clock.run(2000));
      act(() => useSettingsStore.getState().setDock({ baseItemSize: 40 })); // React writes 40px on every item
      expect([items()[0].style.width, items()[0].style.height]).toEqual(["70px", "70px"]);
      expect([items()[8].style.width, items()[8].style.height]).toEqual(["50px", "50px"]);
      fireEvent.pointerLeave(bar());
      act(() => clock.run(2000));
      expect(items().map((el) => el.style.width)).toEqual(Array(9).fill("40px"));
      expect(clock.pending()).toBe(0);
    });

    it("animates items that join the bar later", () => {
      motion(false); placeBar(600); const clock = frameClock();
      useAuthStore.setState({ role: "user" });
      render(<Dock />);
      act(() => useAuthStore.setState({ role: "admin" }));
      fireEvent.pointerMove(bar(), { clientX: 845.5, clientY: 750 }); // Users' rest centre
      act(() => clock.run(2000));
      expect(width(items()[8])).toBe(70);
    });

    it.each([
      ["bottom", "translateY", -1],
      ["top", "translateY", 1],
      ["left", "translateX", 1],
      ["right", "translateX", -1],
    ] as const)("hops a %s dock's tile away from its edge, then lands", (position, axis, sign) => {
      motion(false); const clock = frameClock();
      useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, position } });
      render(<Dock />);
      fireEvent.click(items()[3]);
      act(() => clock.run(192)); // the top of the arc, 0.19 s in
      const [, name, px] = /^(\w+)\((-?[\d.]+)px\)$/.exec(tile(items()[3]).style.transform) ?? [];
      expect(name).toBe(axis);
      expect(Number(px)).toBeCloseTo(sign * 20, 0); // 0.4 × 50
      act(() => clock.run(600));
      expect(tile(items()[3]).style.transform).toBe("");
      expect(items()[3]).not.toHaveAttribute("data-hop");
    });

    it("hops 0.4 × the tile's current size high and carries on when clicked in the air", () => {
      motion(false); placeBar(600); const clock = frameClock();
      render(<Dock />);
      fireEvent.pointerMove(bar(), { clientX: 534.5, clientY: 750 }); // Network's rest centre
      act(() => clock.run(2000));
      fireEvent.click(items()[3]);
      const lift = () => -Number.parseFloat(/-?[\d.]+/.exec(tile(items()[3]).style.transform)?.[0] ?? "0");
      act(() => clock.run(96));
      const before = lift();
      fireEvent.click(items()[3]);
      act(() => clock.run(16));
      expect(lift()).toBeGreaterThan(before);
      act(() => clock.run(80)); // the top of the arc, 0.19 s in
      expect(lift()).toBeCloseTo(28, 0); // 0.4 × 70
    });

    it.each([
      ["bottom", [600, 748], [600, 746]],
      ["top", [600, 88], [600, 90]],
      ["left", [52, 400], [54, 400]],
      ["right", [1148, 400], [1146, 400]],
    ] as const)("reveals a %s dock within EDGE + 0.6 × thickness of its edge", (position, inside, outside) => {
      motion(true); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      act(() => useSettingsStore.getState().setDock({ position })); // the strip follows the dock
      fireEvent.pointerMove(window, { clientX: inside[0], clientY: inside[1] }); // 52px in
      expect(bar()).not.toHaveAttribute("data-hidden");
      fireEvent.pointerMove(window, { clientX: outside[0], clientY: outside[1] }); // 54px in
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
    });

    it("stays shown while the pointer is on it, and shows again after hiding", () => {
      motion(true); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      fireEvent.pointerMove(window, { clientX: 600, clientY: 790 });
      fireEvent.pointerMove(bar(), { clientX: 600, clientY: 700 }); // up onto the bar, out of the strip
      act(() => { vi.advanceTimersByTime(1000); });
      expect(bar()).not.toHaveAttribute("data-hidden");
      fireEvent.pointerLeave(bar());
      act(() => { vi.advanceTimersByTime(200); });
      fireEvent.pointerMove(window, { clientX: 600, clientY: 790 }); // back to the edge in time
      act(() => { vi.advanceTimersByTime(1000); });
      expect(bar()).not.toHaveAttribute("data-hidden");
      fireEvent.pointerMove(window, { clientX: 600, clientY: 300 });
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
      fireEvent.pointerMove(window, { clientX: 600, clientY: 790 });
      expect(bar()).not.toHaveAttribute("data-hidden");
    });

    it("hides again once focus leaves or its menu closes", () => {
      motion(true); vi.useFakeTimers(); autoHide();
      render(<Dock />);
      act(() => items()[0].focus());
      act(() => items()[0].blur());
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
      act(() => items()[0].focus());
      fireEvent.contextMenu(items()[0]);
      fireEvent.pointerDown(document.body); // a click elsewhere closes the menu
      act(() => { vi.advanceTimersByTime(300); });
      expect(bar()).toHaveAttribute("data-hidden");
    });

    it("stays shown once its menu's Hide minimises the maximised window", () => {
      motion(true); vi.useFakeTimers(); // auto-hide is off
      render(<><button type="button">Elsewhere</button><Dock /></>);
      act(() => {
        const store = useWindowStore.getState();
        store.openWindow("terminal");
        const { id, x, y, width, height } = useWindowStore.getState().windows[0];
        store.maximizeWindow(id, { x, y, width, height });
      });
      expect(bar()).toHaveAttribute("data-hidden");
      const terminal = within(bar()).getByRole("button", { name: "Terminal (open)" });
      act(() => terminal.focus()); // keyboard focus shows the hidden dock
      fireEvent.keyDown(terminal, { key: "ArrowUp" });
      fireEvent.click(screen.getByRole("menuitem", { name: "Hide" }));
      // Minimised, the window still has its maximised flag.
      expect(useWindowStore.getState().windows[0]).toMatchObject({ isMinimized: true, isMaximized: true });
      act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
      act(() => { vi.advanceTimersByTime(1000); });
      expect(bar()).not.toHaveAttribute("data-hidden");
    });

    it("fits again on resize, leaving the menubar out of a side dock's room", () => {
      motion(true);
      render(<Dock />);
      expect(width(items()[0])).toBe(50);
      setViewport(400, 800);
      fireEvent(window, new Event("resize"));
      expect(width(items()[0])).toBeCloseTo(50 * (376 / (559 + 66.81)), 1);
      act(() => useSettingsStore.getState().setDock({ position: "left" }));
      setViewport(1200, 500);
      fireEvent(window, new Event("resize"));
      expect(width(items()[0])).toBeCloseTo(50 * (440 / (559 + 66.81)), 1); // 500 − 36 − 2 × 12
    });

    it.each([
      ["left", 1280, 600, {}, 540], // 600 − 36 − 2 × 12
      ["bottom", 800, 800, { baseItemSize: 80, magnification: 140, distance: 400, panelHeight: 120, gap: 30 }, 776],
    ] as const)("shrinks a %s dock so that, magnified at its longest, it still fits", (position, w, h, sizes, room) => {
      motion(true); setViewport(w, h);
      useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, ...sizes, position } });
      const centre = position === "left" ? 36 + (h - 36) / 2 : w / 2; // a side dock centres below the menubar
      if (position === "left") placeColumn(centre); else placeBar(centre);
      render(<Dock />);
      const toolbar = bar();
      const tiles = items();
      const css = (name: string) => Number.parseFloat(toolbar.style.getPropertyValue(name));
      // The bar is its items, the gaps between its 10 entries, the separator's 1px and its padding.
      const length = () => tiles.reduce((sum, tile) => sum + width(tile), 0) + 9 * css("--dock-gap") + 1 +
        2 * css("--dock-pad");
      let longest = 0;
      for (let at = centre - room / 2; at <= centre + room / 2; at += 1) { // the pointer along the whole bar
        fireEvent.pointerMove(toolbar, position === "left" ? { clientX: 40, clientY: at } : { clientX: at, clientY: 760 });
        longest = Math.max(longest, length());
      }
      expect(longest).toBeLessThanOrEqual(room + 1); // the separator's 1px does not shrink
      expect(longest).toBeGreaterThan(room - 1); // and it shrinks no more than it has to
    });

    it("hands the hover reach to the CSS: one item's swell plus 8px", () => {
      useSettingsStore.setState({ dock: { ...DOCK_DEFAULTS, baseItemSize: 40, magnification: 100 } });
      render(<Dock />);
      expect(bar().style.getPropertyValue("--dock-hover-reach")).toBe("68px");
    });

    it("clears its hide timer and frame loop when it unmounts", () => {
      motion(false); vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); autoHide();
      const cancel = vi.fn();
      vi.stubGlobal("requestAnimationFrame", () => 7);
      vi.stubGlobal("cancelAnimationFrame", cancel);
      const { unmount } = render(<Dock />);
      fireEvent.pointerMove(window, { clientX: 600, clientY: 790 });
      const pending = vi.getTimerCount();
      fireEvent.pointerMove(window, { clientX: 600, clientY: 300 }); // the hide timer starts
      fireEvent.click(items()[3]); // and a hop asks for a frame
      expect(vi.getTimerCount()).toBe(pending + 1);
      unmount();
      expect(vi.getTimerCount()).toBe(pending);
      expect(cancel).toHaveBeenCalledWith(7);
    });
  });
});
