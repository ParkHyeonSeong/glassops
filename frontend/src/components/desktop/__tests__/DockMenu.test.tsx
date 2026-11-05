import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AppWindow } from "lucide-react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DockMenu from "../DockMenu";
import type { DockMenuRow } from "../dockModel";

const anchor = { left: 475, top: 300, right: 525, bottom: 350 };
function renderMenu(rows: DockMenuRow[], initialFocus: "first" | "none" = "first") {
  const onClose = vi.fn();
  render(<DockMenu rows={rows} anchor={anchor} side="bottom" theme="dark" accentColor="#0a84ff"
    onClose={onClose} initialFocus={initialFocus} />);
  return onClose;
}
const row = (label: string, extra: Partial<Extract<DockMenuRow, { kind: "item" }>> = {}): DockMenuRow =>
  ({ kind: "item", label, onSelect: vi.fn(), ...extra });

describe("DockMenu", () => {
  it("renders checks, shortcuts, separators and disabled rows", () => {
    renderMenu([row("Logs: nginx", { checked: true }), { kind: "separator" }, row("Hide", { disabled: true }),
      row("Reload", { shortcut: "⌘R" })]);
    expect(screen.getByRole("menuitemcheckbox", { name: /Logs: nginx/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitem", { name: /Hide/ })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("⌘R")).toBeInTheDocument();
    expect(screen.getAllByRole("separator")).toHaveLength(1);
    expect(screen.getByRole("menu").closest("[data-dock-menu]")).not.toBeNull();
  });
  it("runs a row on click and closes with select", () => {
    const quit = row("Quit");
    const onClose = renderMenu([quit]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Quit" }));
    expect(quit.kind === "item" && quit.onSelect).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledWith("select");
  });
  it("ignores disabled rows", () => {
    const hide = row("Hide", { disabled: true });
    const onClose = renderMenu([hide]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Hide" }));
    expect(hide.kind === "item" && hide.onSelect).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
  it("moves with the arrows, wrapping and skipping separators and disabled rows", () => {
    renderMenu([row("A"), { kind: "separator" }, row("B", { disabled: true }), row("C")]);
    const a = screen.getByRole("menuitem", { name: "A" });
    const c = screen.getByRole("menuitem", { name: "C" });
    expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: "ArrowDown" }); expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: "ArrowDown" }); expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: "ArrowUp" }); expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: "Home" }); expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: "End" }); expect(c).toHaveFocus();
  });
  it("activates the focused row with Enter", () => {
    const open = row("Open");
    const onClose = renderMenu([open]);
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Open" }), { key: "Enter" });
    expect(open.kind === "item" && open.onSelect).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledWith("select");
  });
  it("closes on Escape and Tab", () => {
    const onClose = renderMenu([row("A")]);
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "A" }), { key: "Escape" });
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "A" }), { key: "Tab" });
    expect(onClose.mock.calls).toEqual([["escape"], ["tab"]]);
  });
  it("opens a submenu with ArrowRight and closes only it with ArrowLeft or Escape", () => {
    const inner = row("Inner");
    const onClose = renderMenu([row("More", { onSelect: undefined, items: [inner] })]);
    const more = screen.getByRole("menuitem", { name: /More/ });
    expect(more).toHaveAttribute("aria-haspopup", "menu");
    fireEvent.keyDown(more, { key: "ArrowRight" });
    expect(screen.getAllByRole("menu")).toHaveLength(2);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitem", { name: "Inner" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Inner" }), { key: "ArrowLeft" });
    expect(screen.getAllByRole("menu")).toHaveLength(1);
    expect(more).toHaveFocus();
    fireEvent.keyDown(more, { key: "ArrowRight" });
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Inner" }), { key: "Escape" });
    expect(screen.getAllByRole("menu")).toHaveLength(1);
    expect(onClose).not.toHaveBeenCalled();
  });
  it("closes on outside pointerdown and on resize", () => {
    const onClose = renderMenu([row("A")]);
    fireEvent.pointerDown(document.body);
    fireEvent(window, new Event("resize"));
    expect(onClose.mock.calls).toEqual([["outside"], ["dismiss"]]);
  });
  it("applies the placement's max height so long menus scroll", () => {
    renderMenu(Array.from({ length: 40 }, (_, i) => row(`Logs: c${i}`)));
    expect(screen.getByRole("menu").style.maxHeight).toBe(`${300 - 25}px`);
  });
});

// ===== In detail: markup, keys, submenus, pointer, closing, placement and motion =====

type MenuExtras = Partial<
  Pick<ComponentProps<typeof DockMenu>, "side" | "theme" | "accentColor" | "anchor" | "initialFocus">
>;
const menuEl = (rows: DockMenuRow[], onClose: () => void, extra: MenuExtras = {}) => (
  <DockMenu rows={rows} anchor={anchor} side="bottom" theme="dark" accentColor="#0a84ff"
    onClose={onClose} {...extra} />
);
// Like renderMenu, with every prop overridable and a rerender that keeps the rest.
function mount(rows: DockMenuRow[], extra: MenuExtras = {}) {
  const onClose = vi.fn();
  const view = render(menuEl(rows, onClose, extra));
  return {
    onClose,
    unmount: view.unmount,
    rerender: (more: MenuExtras) => view.rerender(menuEl(rows, onClose, { ...extra, ...more })),
  };
}
const submenuRow = (label: string, items: DockMenuRow[], extra: Partial<Extract<DockMenuRow, { kind: "item" }>> = {}) =>
  row(label, { onSelect: undefined, items, ...extra });
const item = (name: string | RegExp) => screen.getByRole("menuitem", { name });
const menus = () => screen.getAllByRole("menu");
const liveRoot = () => document.querySelector<HTMLElement>("[data-dock-menu]")!;
const lit = () => Array.from(document.querySelectorAll("[data-active]")).map((el) => el.textContent);
// The inert copies a closing menu leaves in <body> to fade out.
const copies = () => Array.from(document.querySelectorAll<HTMLElement>('body > [aria-hidden="true"]'));

// jsdom has no layout: a test that needs one gives the menus a size and the rows a place, 24px
// tall from 5px down, one under the other.
function stubLayout(width = 200, height = 120) {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(width);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.hasAttribute("data-row") ? 24 : height;
  });
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(function (this: HTMLElement) {
    return this.hasAttribute("data-row") ? 5 + 24 * Number(this.dataset.row) : 0;
  });
}

// jsdom has no Element.animate. This one records each call and lets a test end the animations.
function stubAnimate() {
  const calls: { target: Element; keyframes: Keyframe[]; options: KeyframeAnimationOptions; cancel: () => void }[] = [];
  const ends: (() => void)[] = [];
  Object.defineProperty(Element.prototype, "animate", {
    configurable: true,
    writable: true,
    value(this: Element, keyframes: Keyframe[], options: KeyframeAnimationOptions) {
      const cancel = vi.fn();
      calls.push({ target: this, keyframes, options, cancel });
      return { finished: new Promise<void>((resolve) => ends.push(resolve)), cancel };
    },
  });
  return { calls, endAll: () => ends.forEach((end) => end()) };
}
const reduceMotion = () =>
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("prefers-reduced-motion"), media: query }));
afterEach(() => {
  delete (Element.prototype as { animate?: unknown }).animate;
  vi.unstubAllGlobals();
});

describe("DockMenu markup", () => {
  it("shows a check only on checked rows and keeps each row's text to its label and shortcut", () => {
    mount([row("Docker", { checked: true, icon: AppWindow }), row("Logs", { checked: false }),
      row("Reload", { shortcut: "⌘R" }), submenuRow("More", [row("Inner")])]);
    const docker = screen.getByRole("menuitemcheckbox", { name: "Docker" });
    const logs = screen.getByRole("menuitemcheckbox", { name: "Logs" });
    expect(docker.textContent).toBe("Docker");
    expect(item(/Reload/).textContent).toBe("Reload⌘R");
    expect(item("More").textContent).toBe("More");
    // The marks are SVG icons that assistive tech skips, never text.
    expect(docker.querySelectorAll('svg[aria-hidden="true"]')).toHaveLength(2); // the check and the icon
    expect(logs.querySelector("svg")).toBeNull();
    expect(item(/Reload/).querySelector("svg")).toBeNull();
    expect(item("More").querySelectorAll("svg")).toHaveLength(1); // the chevron
  });
  it("sets ARIA state per row and names a submenu after the row that opened it", () => {
    mount([row("Plain"), row("Off", { checked: false }), submenuRow("More", [row("Inner")])]);
    expect(screen.getByRole("menuitemcheckbox", { name: "Off" })).toHaveAttribute("aria-checked", "false");
    for (const attr of ["aria-checked", "aria-disabled", "aria-haspopup", "aria-expanded"]) {
      expect(item("Plain")).not.toHaveAttribute(attr);
    }
    expect(item("More")).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    expect(item("More")).toHaveAttribute("aria-expanded", "true");
    expect(menus()[1]).toHaveAttribute("aria-labelledby", item("More").id);
    expect(menus()[0]).not.toHaveAttribute("aria-labelledby");
  });
  it("sits in document.body and carries the theme and the accent", () => {
    mount([row("A")], { theme: "light", accentColor: "#ff00aa" });
    expect(liveRoot().parentElement).toBe(document.body);
    expect(liveRoot()).toHaveAttribute("data-theme", "light");
    expect(liveRoot().style.getPropertyValue("--dock-menu-accent")).toBe("#ff00aa");
  });
  it("keeps its keys, mouse-down and context menu to itself", () => {
    const outerKey = vi.fn();
    render(<div onKeyDown={outerKey}>{menuEl([row("A")], vi.fn())}</div>);
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    fireEvent.keyDown(item("A"), { key: "x" });
    expect(outerKey).not.toHaveBeenCalled();
    expect(fireEvent.mouseDown(item("A"))).toBe(false); // focus stays on the active row
    expect(fireEvent.contextMenu(item("A"))).toBe(false); // no native menu on top of it
  });
  it("draws one tail, on the first level only", () => {
    mount([submenuRow("More", [row("Inner")])]);
    expect(liveRoot().querySelectorAll(".dock-menu-tail")).toHaveLength(1);
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    const [first, second] = Array.from(liveRoot().querySelectorAll<HTMLElement>(".dock-menu"));
    expect(first).toHaveAttribute("data-side", "bottom");
    expect(second).not.toHaveAttribute("data-side");
    expect(liveRoot().querySelectorAll(".dock-menu-tail")).toHaveLength(1);
    expect(first.querySelector(".dock-menu-tail")).not.toBeNull();
  });
});

describe("DockMenu keyboard", () => {
  it("lights the first enabled row on open", () => {
    mount([row("Gone", { disabled: true }), { kind: "separator" }, row("First"), row("Second")]);
    expect(item("First")).toHaveFocus();
    expect(lit()).toEqual(["First"]);
  });
  it("with initialFocus none focuses the list and lights nothing, and Enter picks nothing", () => {
    const first = row("First");
    const { onClose } = mount([first, row("Last"), row("Off", { disabled: true })], { initialFocus: "none" });
    expect(menus()[0]).toHaveFocus();
    expect(lit()).toEqual([]);
    fireEvent.keyDown(menus()[0], { key: "Enter" });
    expect(first.kind === "item" && first.onSelect).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(menus()[0], { key: "ArrowDown" });
    expect(item("First")).toHaveFocus();
  });
  it("starts from the last enabled row on ArrowUp when no row is lit", () => {
    mount([row("First"), row("Last"), row("Off", { disabled: true })], { initialFocus: "none" });
    fireEvent.keyDown(menus()[0], { key: "ArrowUp" });
    expect(item("Last")).toHaveFocus();
  });
  it("moves down and up in the right direction", () => {
    mount([row("A"), row("B"), row("C")]);
    fireEvent.keyDown(item("A"), { key: "ArrowDown" }); expect(item("B")).toHaveFocus();
    fireEvent.keyDown(item("B"), { key: "ArrowDown" }); expect(item("C")).toHaveFocus();
    fireEvent.keyDown(item("C"), { key: "ArrowUp" }); expect(item("B")).toHaveFocus();
    expect(lit()).toEqual(["B"]);
  });
  it("sends Home and End to the first and last enabled rows", () => {
    mount([row("Off1", { disabled: true }), row("A"), row("B"), row("Off2", { disabled: true })]);
    fireEvent.keyDown(item("A"), { key: "End" }); expect(item("B")).toHaveFocus();
    fireEvent.keyDown(item("B"), { key: "Home" }); expect(item("A")).toHaveFocus();
  });
  it("keeps the only enabled row lit when arrowing", () => {
    mount([row("Show All Windows", { disabled: true }), row("Hide", { disabled: true }),
      { kind: "separator" }, row("Open")]);
    fireEvent.keyDown(item("Open"), { key: "ArrowDown" }); expect(item("Open")).toHaveFocus();
    fireEvent.keyDown(item("Open"), { key: "ArrowUp" }); expect(item("Open")).toHaveFocus();
  });
  it("activates with Space, never a disabled row", () => {
    const go = row("Go");
    const { onClose } = mount([go, row("Off", { disabled: true })]);
    fireEvent.keyDown(item("Off"), { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(item("Go"), { key: " " });
    expect(go.kind === "item" && go.onSelect).toHaveBeenCalledOnce();
    expect(onClose.mock.calls).toEqual([["select"]]);
  });
  it("leaves ArrowLeft alone on the first level and does not open a disabled submenu", () => {
    const { onClose } = mount([submenuRow("More", [row("Inner")]),
      submenuRow("Locked", [row("Never")], { disabled: true })]);
    fireEvent.keyDown(item("More"), { key: "ArrowLeft" });
    fireEvent.keyDown(item("Locked"), { key: "ArrowRight" });
    expect(onClose).not.toHaveBeenCalled();
    expect(menus()).toHaveLength(1);
    expect(item("More")).toHaveFocus();
  });
  it("prevents the default of the keys it handles, Tab included, and of no others", () => {
    mount([row("A")]);
    expect(fireEvent.keyDown(item("A"), { key: "ArrowDown" })).toBe(false);
    expect(fireEvent.keyDown(item("A"), { key: "Tab" })).toBe(false);
    expect(fireEvent.keyDown(item("A"), { key: "a" })).toBe(true);
  });
});

// The dock rebuilds the rows whenever its windows change, so they can change while the menu is open.
describe("DockMenu rows changing", () => {
  it("keeps focus on a row that is still enabled, else moves it to the nearest enabled row above", () => {
    const onClose = vi.fn();
    const view = render(menuEl([row("A"), row("B"), row("C"), row("D"), row("E")], onClose));
    const show = (rows: DockMenuRow[]) => view.rerender(menuEl(rows, onClose));
    fireEvent.keyDown(item("A"), { key: "End" });
    show([row("A"), row("B"), row("C")]); // two rows past the end
    expect(item("C")).toHaveFocus();
    expect(lit()).toEqual(["C"]);
    show([row("A"), row("B"), row("C"), row("E")]);
    expect(item("C")).toHaveFocus();
    show([row("A"), row("B"), { kind: "separator" }, row("E")]);
    expect(item("B")).toHaveFocus();
    show([row("A", { disabled: true }), row("B", { disabled: true }), row("C"), row("E")]); // wraps round
    expect(item("E")).toHaveFocus();
    show([row("Off", { disabled: true })]); // nothing to land on: the list itself
    expect(menus()[0]).toHaveFocus();
    expect(lit()).toEqual([]);
    show([row("A")]); // and it stays there
    expect(menus()[0]).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
  });
  it("hands focus to the row that opened a submenu that is gone", () => {
    const onClose = vi.fn();
    const view = render(menuEl([row("A"), submenuRow("More", [row("Inner")])], onClose));
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    expect(item("Inner")).toHaveFocus();
    view.rerender(menuEl([row("A"), row("More")], onClose));
    expect(menus()).toHaveLength(1);
    expect(item("More")).toHaveFocus();
    expect(lit()).toEqual(["More"]);
  });
});

describe("DockMenu submenus", () => {
  it("opens on Enter into the first enabled row, but a click leaves focus on the opening row", () => {
    const { onClose } = mount([submenuRow("More", [row("Off", { disabled: true }), row("Inner1"), row("Inner2")])]);
    fireEvent.click(item("More"));
    expect(menus()).toHaveLength(2);
    expect(item("More")).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(item("More"), { key: "Enter" });
    expect(item("Inner1")).toHaveFocus();
    expect(lit()).toEqual(["More", "Inner1"]); // the opening row stays lit
  });
  it("closes the whole menu on Tab and returns focus to the opening row on Escape", () => {
    const { onClose } = mount([submenuRow("More", [row("Inner")])]);
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    fireEvent.keyDown(item("Inner"), { key: "Escape" });
    expect(item("More")).toHaveFocus();
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    expect(fireEvent.keyDown(item("Inner"), { key: "Tab" })).toBe(false);
    expect(onClose.mock.calls).toEqual([["tab"]]);
  });
  it("closes a submenu the pointer opened when the keys move on", () => {
    mount([submenuRow("More", [row("Inner")]), row("B")]);
    fireEvent.mouseMove(item("More"));
    expect(menus()).toHaveLength(2);
    fireEvent.keyDown(item("More"), { key: "ArrowDown" });
    expect(menus()).toHaveLength(1);
    expect(item("B")).toHaveFocus();
  });
  it("runs a submenu row and closes the whole menu", () => {
    const inner = row("Inner");
    const { onClose } = mount([submenuRow("More", [inner])]);
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    fireEvent.click(item("Inner"));
    expect(inner.kind === "item" && inner.onSelect).toHaveBeenCalledOnce();
    expect(onClose.mock.calls).toEqual([["select"]]);
  });
  it("nests as deep as the rows go and closes one level at a time", () => {
    const deep = row("Deep");
    const { onClose } = mount([row("A"), submenuRow("More", [submenuRow("Mid", [deep])])]);
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    fireEvent.keyDown(item("Mid"), { key: "ArrowRight" });
    expect(menus()).toHaveLength(3);
    expect(item("Deep")).toHaveFocus();
    fireEvent.keyDown(item("Deep"), { key: "Escape" });
    expect(menus()).toHaveLength(2);
    expect(item("Mid")).toHaveFocus();
    fireEvent.keyDown(item("Mid"), { key: "ArrowLeft" });
    expect(menus()).toHaveLength(1);
    expect(item("More")).toHaveFocus();
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    fireEvent.keyDown(item("Mid"), { key: "ArrowRight" });
    fireEvent.keyDown(item("Deep"), { key: "Enter" });
    expect(deep.kind === "item" && deep.onSelect).toHaveBeenCalledOnce();
    expect(onClose.mock.calls).toEqual([["select"]]);
  });
});

describe("DockMenu pointer", () => {
  it("lights the row under the pointer, opens its submenu and follows the pointer from row to row", () => {
    mount([row("A"), submenuRow("More", [row("Inner")]), submenuRow("Other", [row("Deep")]),
      submenuRow("Locked", [row("Never")], { disabled: true })]);
    fireEvent.mouseMove(item("More"));
    expect(menus()).toHaveLength(2);
    expect(item("More")).toHaveFocus(); // the opening row keeps focus
    expect(item("Inner")).not.toHaveFocus();
    fireEvent.mouseMove(item("Inner"));
    expect(item("Inner")).toHaveFocus();
    expect(lit()).toEqual(["More", "Inner"]);
    fireEvent.mouseMove(item("More"));
    expect(menus()).toHaveLength(2);
    expect(item("More")).toHaveFocus();
    expect(lit()).toEqual(["More"]);
    const innerMenu = menus()[1];
    fireEvent.mouseMove(item("Other")); // another submenu replaces it
    expect(menus()).toHaveLength(2);
    expect(menus()[1]).not.toBe(innerMenu);
    expect(screen.queryByRole("menuitem", { name: "Inner" })).toBeNull();
    expect(item("Deep")).toBeInTheDocument();
    fireEvent.mouseMove(item("A"));
    expect(menus()).toHaveLength(1);
    expect(item("A")).toHaveFocus();
    fireEvent.mouseMove(item("Locked")); // a disabled row lights nothing and opens nothing
    expect(menus()).toHaveLength(1);
    expect(lit()).toEqual([]);
    expect(menus()[0]).toHaveFocus();
  });
  it("focuses without scrolling for the pointer, with scrolling for keys, and not again on the same row", () => {
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    mount([row("A"), row("B")]);
    fireEvent.mouseMove(item("B"));
    expect(focus.mock.lastCall?.[0]).toEqual({ preventScroll: true });
    const calls = focus.mock.calls.length;
    fireEvent.mouseMove(item("B"));
    expect(focus.mock.calls.length).toBe(calls);
    fireEvent.keyDown(item("B"), { key: "ArrowUp" });
    expect(focus.mock.lastCall?.[0]?.preventScroll).toBeFalsy();
  });
  it("slides the highlight bar to the lit row and hides it when no row is lit", () => {
    stubLayout();
    mount([row("A"), row("B"), row("Off", { disabled: true })]);
    const bar = document.querySelector<HTMLElement>(".dock-menu-highlight")!;
    expect([bar.style.transform, bar.style.height, bar.style.opacity]).toEqual(["translateY(5px)", "24px", "1"]);
    expect(bar.style.transition).toBe(""); // the first placement must not leave transitions off
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    expect(bar.style.transform).toBe("translateY(29px)");
    fireEvent.mouseMove(item("Off"));
    expect(bar.style.opacity).toBe("0");
  });
  it("moves the highlight bar with its transition left on, so it slides", () => {
    stubLayout();
    mount([row("A"), row("B")]);
    const bar = document.querySelector<HTMLElement>(".dock-menu-highlight")!;
    const watch = new MutationObserver(() => {});
    watch.observe(bar, { attributes: true, attributeFilter: ["style"], attributeOldValue: true });
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    const moves = watch.takeRecords();
    watch.disconnect();
    expect(moves.length).toBeGreaterThan(0);
    expect(moves.some((m) => m.oldValue?.includes("transition"))).toBe(false); // never switched off for a move
  });
});

describe("DockMenu closing", () => {
  it("runs the action before closing, and closes even when the action throws", () => {
    const order: string[] = [];
    const onClose = vi.fn(() => { order.push("close"); });
    render(menuEl([row("Go", { onSelect: () => { order.push("select"); } }),
      row("Boom", { onSelect: () => { order.push("boom"); throw new Error("boom"); } })], onClose));
    fireEvent.click(item("Go"));
    expect(order).toEqual(["select", "close"]);
    const swallow = (e: ErrorEvent) => e.preventDefault(); // the throw is the point; keep it out of the run
    window.addEventListener("error", swallow);
    fireEvent.click(item("Boom"));
    window.removeEventListener("error", swallow);
    expect(order).toEqual(["select", "close", "boom", "close"]);
  });
  it("closes with dismiss on window blur and scroll", () => {
    const { onClose } = mount([row("A")]);
    fireEvent(window, new Event("blur"));
    fireEvent(window, new Event("scroll"));
    expect(onClose.mock.calls).toEqual([["dismiss"], ["dismiss"]]);
  });
  it("stays open for a pointerdown inside it and for its own list scrolling", () => {
    const { onClose } = mount([row("A")]);
    fireEvent.pointerDown(item("A"));
    fireEvent.pointerDown(menus()[0]);
    fireEvent.scroll(menus()[0]);
    expect(onClose).not.toHaveBeenCalled();
  });
  it("hears an outside pointerdown that another handler swallows", () => {
    const { onClose } = mount([row("A")]);
    const other = document.body.appendChild(document.createElement("div"));
    other.addEventListener("pointerdown", (e) => e.stopPropagation());
    fireEvent.pointerDown(other);
    other.remove();
    expect(onClose).toHaveBeenCalledWith("outside");
  });
  it("stops listening once it is unmounted", () => {
    const { onClose, unmount } = mount([row("A")]);
    unmount();
    fireEvent.pointerDown(document.body);
    for (const type of ["resize", "blur", "scroll"]) fireEvent(window, new Event(type));
    expect(onClose).not.toHaveBeenCalled();
  });
  it("reports to the latest onClose", () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = render(menuEl([row("A")], first));
    view.rerender(menuEl([row("A")], second));
    fireEvent.pointerDown(document.body);
    expect(second.mock.calls).toEqual([["outside"]]);
    expect(first).not.toHaveBeenCalled();
  });
});

describe("DockMenu placement", () => {
  it.each([
    ["bottom", "400px", "163px", "100px"],
    ["top", "400px", "367px", "100px"],
    ["left", "542px", "265px", "60px"],
    ["right", "258px", "265px", "60px"],
  ] as const)("places a %s dock's menu from its rendered size", (side, left, top, tail) => {
    stubLayout(200, 120);
    mount([row("A")], { side });
    const wrap = menus()[0].parentElement!;
    expect(wrap).toHaveAttribute("data-side", side);
    const placed = [wrap.style.left, wrap.style.top, wrap.style.getPropertyValue("--dock-menu-tail")];
    expect(placed).toEqual([left, top, tail]);
  });
  it("keeps the menu inside the viewport and slides the tail to the item", () => {
    stubLayout(200, 120);
    mount([row("A")], { anchor: { left: 980, top: 300, right: 1020, bottom: 350 } });
    const wrap = menus()[0].parentElement!;
    expect([wrap.style.left, wrap.style.getPropertyValue("--dock-menu-tail")]).toEqual(["816px", "181px"]);
  });
  it.each([[25], [20]])("caps the height only when there is room above an anchor at %ipx", (top) => {
    mount([row("A")], { anchor: { left: 475, top, right: 525, bottom: top + 50 } });
    expect(menus()[0].style.maxHeight).toBe(""); // room 0 or less: no cap rather than a collapsed menu
  });
  it("places again when the anchor moves, measuring the menu at its natural height", () => {
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      const cap = parseFloat(this.querySelector<HTMLElement>(".dock-menu-list")?.style.maxHeight ?? "");
      return Number.isNaN(cap) ? 300 : Math.min(300, cap);
    });
    const { rerender } = mount([row("A")]);
    const wrap = menus()[0].parentElement!;
    expect([wrap.style.top, menus()[0].style.maxHeight]).toEqual(["8px", "275px"]);
    rerender({ anchor: { left: 475, top: 500, right: 525, bottom: 550 } });
    expect([wrap.style.top, menus()[0].style.maxHeight]).toEqual(["183px", "475px"]);
  });
  it("puts a scrolled list back where it was when it places again", () => {
    mount([submenuRow("More", [row("Inner")])]);
    // jsdom has no layout to clamp the scroll position while the height cap is lifted for
    // measuring, so watch for the put-back instead.
    const list = menus()[0];
    const puts: number[] = [];
    Object.defineProperty(list, "scrollTop", {
      configurable: true,
      get: () => 120,
      set: (v: number) => { puts.push(v); },
    });
    fireEvent.keyDown(item("More"), { key: "ArrowRight" }); // opening a level places every level again
    expect(puts).toEqual([120]);
  });
  it("opens a submenu beside its row, to the right or, with no room, to the left", () => {
    const at = (left: number, top: number) => ({ left, top, right: left + 50, bottom: top + 50 });
    // The menu sits 17px above its anchor, so its top is anchor top - 17 - 80; the opening row
    // is the second one, 29px below that.
    const cases: [number, NonNullable<MenuExtras["anchor"]>, number, string, string, boolean][] = [
      // [menu width, anchor, list scrolled by, submenu left, submenu top, flipped]
      [150, anchor, 0, "571px", "227px", false], // room on the right, level with its row
      [150, at(774, 300), 0, "578px", "227px", true], // would end 4px past the margin: flips
      [800, anchor, 0, "8px", "227px", true], // flipped, held at the margin
      [150, at(475, 760), 0, "571px", "680px", false], // held above the bottom margin
      [150, anchor, 300, "571px", "8px", false], // row scrolled out of sight: held below the top margin
    ];
    for (const [width, from, scrolled, left, top, flipped] of cases) {
      stubLayout(width, 80);
      const { unmount } = mount([row("A"), submenuRow("More", [row("Inner")])], { anchor: from });
      fireEvent.keyDown(item("A"), { key: "ArrowDown" });
      Object.defineProperty(menus()[0], "scrollTop", { configurable: true, get: () => scrolled, set: () => {} });
      fireEvent.keyDown(item("More"), { key: "ArrowRight" });
      const wrap = menus()[1].parentElement!;
      expect([wrap.style.left, wrap.style.top]).toEqual([left, top]);
      expect(wrap.style.transformOrigin.startsWith("100%")).toBe(flipped);
      expect(menus()[1].style.maxHeight).toBe("752px");
      unmount();
    }
  });
  it("places a submenu of a submenu beside its own parent", () => {
    stubLayout(150, 80);
    mount([row("A"), submenuRow("More", [submenuRow("Mid", [row("Deep")])])]);
    fireEvent.keyDown(item("A"), { key: "ArrowDown" });
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    fireEvent.keyDown(item("Mid"), { key: "ArrowRight" });
    const [, second, third] = menus().map((list) => list.parentElement!);
    expect([second.style.left, second.style.top]).toEqual(["571px", "227px"]);
    expect([third.style.left, third.style.top]).toEqual(["717px", "227px"]);
  });
});

describe("DockMenu motion", () => {
  it("grows the menu in from the tail and fades its glass layers, never the menu as a whole", () => {
    const { calls } = stubAnimate();
    const { unmount } = mount([row("A")]);
    const list = menus()[0];
    const wrap = list.parentElement!;
    expect(calls.map((c) => c.target)).toEqual([wrap, list, wrap.querySelector(".dock-menu-tail")]);
    expect(calls[0].keyframes).toEqual([{ transform: "scale(0.82)" }, { transform: "scale(1)" }]);
    expect(calls[0].options).toMatchObject({ duration: 340, easing: "cubic-bezier(0.2, 1.25, 0.4, 1)" });
    for (const fade of calls.slice(1)) {
      expect(fade.keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }]);
      expect(fade.options).toMatchObject({ duration: 160, easing: "ease-out" });
    }
    unmount();
    for (const c of calls) expect(c.cancel).toHaveBeenCalledOnce();
  });
  it("opens a submenu with a shorter grow from 0.94 and no tail", () => {
    const { calls } = stubAnimate();
    mount([submenuRow("More", [row("Inner")])]);
    const before = calls.length;
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    const wrap = menus()[1].parentElement!;
    expect(calls.slice(before).map((c) => c.target)).toEqual([wrap, menus()[1]]);
    expect(calls[before].keyframes).toEqual([{ transform: "scale(0.94)" }, { transform: "scale(1)" }]);
    expect(calls[before].options).toMatchObject({ duration: 220 });
    expect(calls[before + 1].keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }]);
    expect(calls[before + 1].options).toMatchObject({ duration: 120 });
  });
  it("only fades, briefly, for reduced motion", () => {
    reduceMotion();
    const { calls } = stubAnimate();
    mount([row("A")]);
    expect(calls).toHaveLength(2);
    for (const c of calls) {
      expect(c.keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }]);
      expect(c.options).toMatchObject({ duration: 100 });
    }
  });
  it("closes at once while a detached copy fades out, then drops the copy", async () => {
    const { calls, endAll } = stubAnimate();
    const { onClose } = mount([row("A")]);
    const entered = calls.length;
    fireEvent.keyDown(item("A"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledWith("escape"); // the fade does not hold the close back
    const ghost = document.body.lastElementChild as HTMLElement;
    expect(ghost).not.toBe(liveRoot());
    expect(ghost).toHaveAttribute("aria-hidden", "true");
    expect(ghost.inert).toBe(true);
    expect(document.querySelectorAll("[data-dock-menu]")).toHaveLength(1); // only the live menu answers to it
    const [shrink, ...fades] = calls.slice(entered);
    expect(shrink.target).toBe(ghost.querySelector(".dock-menu"));
    expect(shrink.keyframes).toEqual([{ transform: "scale(1)" }, { transform: "scale(0.96)" }]);
    expect(shrink.options).toMatchObject({ duration: 150, easing: "ease-in", fill: "forwards" });
    expect(fades.map((c) => c.target)).toEqual(
      [ghost.querySelector(".dock-menu-list"), ghost.querySelector(".dock-menu-tail")]);
    for (const fade of fades) {
      expect(fade.keyframes).toEqual([{ opacity: 1 }, { opacity: 0 }]);
      expect(fade.options).toMatchObject({ duration: 150, easing: "ease-in", fill: "forwards" });
    }
    expect(ghost).toBeInTheDocument();
    endAll();
    await waitFor(() => expect(ghost).not.toBeInTheDocument());
  });
  it("leaves one copy however many times it is told to close", async () => {
    const { endAll } = stubAnimate();
    mount([row("A")]);
    fireEvent.pointerDown(document.body);
    fireEvent(window, new Event("blur"));
    expect(copies()).toHaveLength(1);
    endAll();
    await waitFor(() => expect(copies()).toHaveLength(0));
  });
  it("fades the copy out without scaling for reduced motion", async () => {
    reduceMotion();
    const { calls, endAll } = stubAnimate();
    mount([row("A")]);
    const entered = calls.length;
    fireEvent.keyDown(item("A"), { key: "Escape" });
    const out = calls.slice(entered);
    expect(out).toHaveLength(2);
    for (const c of out) {
      expect(c.keyframes).toEqual([{ opacity: 1 }, { opacity: 0 }]);
      expect(c.options).toMatchObject({ duration: 100 });
    }
    endAll();
    await waitFor(() => expect(copies()).toHaveLength(0));
  });
  it("keeps each list's scroll position on the fading copy", async () => {
    const { endAll } = stubAnimate();
    mount([submenuRow("More", [row("Inner")])]);
    fireEvent.keyDown(item("More"), { key: "ArrowRight" });
    const [outer, inner] = menus();
    outer.scrollTop = 40;
    inner.scrollTop = 15;
    fireEvent.keyDown(item("Inner"), { key: "Tab" }); // closes every level
    const [copy] = copies();
    expect(Array.from(copy.querySelectorAll<HTMLElement>(".dock-menu-list"), (list) => list.scrollTop)).toEqual([40, 15]);
    endAll();
    await waitFor(() => expect(copies()).toHaveLength(0));
  });
});
