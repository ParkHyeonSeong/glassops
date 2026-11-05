import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, MouseEvent, PointerEvent } from "react";
import {
  BarChart3,
  Box,
  Container,
  Cpu,
  FileText,
  Globe,
  ListTree,
  Settings,
  TerminalSquare,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useActiveAlerts } from "../../hooks/useActiveAlerts";
import { useAuthStore } from "../../stores/authStore";
import { DOCK_SPRINGS, useSettingsStore } from "../../stores/settingsStore";
import type { DockPosition } from "../../stores/settingsStore";
import { APP_DEFINITIONS, MENUBAR_HEIGHT, useWindowStore } from "../../stores/windowStore";
import DockMenu from "./DockMenu";
import type { DockMenuCloseReason } from "./DockMenu";
import { buildAppMenu, buildDockEntries } from "./dockModel";
import type { DockEntry, DockMenuActions } from "./dockModel";
import {
  DOCK_EDGE,
  dockMetrics,
  fitScale,
  isSettled,
  magnifiedSize,
  peakSwell,
  startHop,
  stepHop,
  stepSpring,
} from "./dockMotion";
import type { DockMetrics, HopState, SpringState } from "./dockMotion";

// The desktop dock: a glass bar on one screen edge with a tile per launcher app, the
// separator before Settings, open-app dots, the System Monitor alert badge and a context
// menu per app (right-click, touch/pen long press, or the keyboard). The bar is a toolbar
// with one Tab stop. Its glass is a layer behind the items, never their ancestor, because
// tiles and labels carry their own backdrop blur (see the Dock block in global.css).
// The menu renders beside the toolbar, not inside it: events from the portalled menu bubble
// through the React tree, so inside an item they would also reach the item's handlers.
// Items swell around the pointer or the keyboard-focused item and hop when clicked; that
// motion runs outside React (createDockMotion). The bar slides off its edge while hidden
// (auto-hide or a maximised window) and shrinks when the viewport is too small for it.

const ICON_MAP: Record<string, LucideIcon> = {
  BarChart3,
  Cpu,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
  Users,
};

const FallbackIcon = Box;

const LONG_PRESS_MS = 450;
const LONG_PRESS_SLOP = 6; // px a held touch or pen may drift and still count as a long press

// The arrow key that points away from the screen edge opens the focused item's menu.
const MENU_KEY: Record<DockPosition, string> = {
  bottom: "ArrowUp",
  top: "ArrowDown",
  left: "ArrowRight",
  right: "ArrowLeft",
};

// What the menu rows run, bound to the window store. They read the store when they run, so
// this object never changes and the rows only need rebuilding when the windows do.
const WINDOW_ACTIONS: DockMenuActions = {
  focusWindow: (id) => useWindowStore.getState().focusWindow(id),
  restoreWindow: (id) => useWindowStore.getState().restoreWindow(id),
  minimizeWindow: (id) => useWindowStore.getState().minimizeWindow(id),
  closeWindow: (id) => useWindowStore.getState().closeWindow(id),
  openWindow: (appId) => useWindowStore.getState().openWindow(appId),
};

interface OpenMenu {
  appId: string;
  anchor: { left: number; top: number; right: number; bottom: number }; // the item, in viewport px
  initialFocus: "first" | "none"; // "first" from the keyboard, "none" from a pointer
}

// A touch or pen held on an item, waiting to become a long press.
interface Press {
  pointerId: number;
  x: number;
  y: number;
  timer: ReturnType<typeof setTimeout>;
}

const DIVIDER_LENGTH = 1; // px a separator takes along the bar, as .dock-divider draws it
const HOVER_REACH = 8; // px the hover region reaches past the bar, on top of one item's swell
const REVEAL_DEPTH = 0.6; // the strip along the dock's edge that reveals it: EDGE + this × thickness
const HIDE_DELAY_MS = 300; // the dock hides this long after the last reason to show it ends
const FIRST_FRAME = 1 / 60; // s, the step of a run's first frame, which has no earlier timestamp
const LONGEST_FRAME = 0.1; // s, a longer frame (say, after a background tab) counts as this
// Pressed on their own these are not keyboard navigation (see onKey in createDockMotion).
const MODIFIER_KEYS = new Set(["Alt", "AltGraph", "CapsLock", "Control", "Meta", "Shift"]);

function reducedMotion(): boolean {
  return globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

// Where the items sit along the bar when nothing is magnified, worked out from the metrics
// rather than measured, so the swell cannot feed back on itself: each app's centre from the
// bar's start, and the bar's length. Apps take `base`, separators DIVIDER_LENGTH, with `gap`
// between neighbours and `pad` at both ends.
function restLayout(
  entries: DockEntry[],
  metrics: DockMetrics,
): { centres: Map<string, number>; length: number } {
  const centres = new Map<string, number>();
  let at = metrics.pad;
  entries.forEach((entry, index) => {
    if (index > 0) at += metrics.gap;
    if (entry.kind === "separator") {
      at += DIVIDER_LENGTH;
      return;
    }
    centres.set(entry.app.id, at + metrics.base / 2);
    at += metrics.base;
  });
  return { centres, length: at + metrics.pad };
}

// How far the pointer is from the line the dock sits against, measured into the screen.
function edgeDistance(side: DockPosition, x: number, y: number): number {
  switch (side) {
    case "bottom":
      return window.innerHeight - y;
    case "top":
      return y - MENUBAR_HEIGHT;
    case "left":
      return x;
    case "right":
      return window.innerWidth - x;
  }
}

function readViewport(): { width: number; height: number } {
  return { width: window.innerWidth, height: window.innerHeight };
}

// A hop lifts the tile away from the screen edge.
const LIFT: Record<DockPosition, (px: number) => string> = {
  bottom: (px) => `translateY(${-px}px)`,
  top: (px) => `translateY(${px}px)`,
  left: (px) => `translateX(${px}px)`,
  right: (px) => `translateX(${-px}px)`,
};

// What the motion engine needs from the latest render.
interface MotionLayout {
  bar: HTMLElement | null;
  centres: Map<string, number>; // rest centre of each app id, from the bar's start, in bar order
  length: number; // the bar's rest length
  metrics: DockMetrics;
  spring: (typeof DOCK_SPRINGS)[keyof typeof DOCK_SPRINGS];
  position: DockPosition;
}

// The magnification and the click hop, outside React so nothing re-renders per frame. Each
// input (the pointer over the bar, keyboard focus, an open menu) sets target sizes; one
// requestAnimationFrame loop moves every item's spring toward its target and writes the size
// into the item's inline width and height, and moves hopping tiles. The loop runs only while
// something moves. With reduced motion, sizes jump to their targets and nothing hops.
function createDockMotion() {
  let layout: MotionLayout | null = null;
  let items = new Map<string, HTMLElement>();
  const springs = new Map<string, SpringState>();
  const targets = new Map<string, number>();
  const hops = new Map<string, HopState>();
  let pointer: number | null = null; // along the bar from its rest start; null when not over it
  let focused: string | null = null; // the item keyboard focus is on
  // The last input was keyboard navigation (a key with no Meta/Ctrl/Alt that is not a bare
  // modifier), not a pointer press, as for :focus-visible.
  let keyboard = true;
  let held = false; // a menu is open: the magnification stays as it was
  let live = false; // between start() and stop()
  let frame: number | null = null;
  let last: number | null = null; // timestamp of the previous frame, ms

  // A key counts as keyboard input unless Meta, Ctrl or Alt is held or it is a modifier on its
  // own, as for :focus-visible: shortcuts and switching apps (Cmd+Tab away and back) are not
  // keyboard navigation. Holding Shift does not disqualify a key: Shift+Tab and Shift+F10 are.
  const onKey = (e: globalThis.KeyboardEvent) => {
    if (e.metaKey || e.ctrlKey || e.altKey || MODIFIER_KEYS.has(e.key)) return;
    keyboard = true;
  };
  // A pointer press also ends the keyboard focus's swell: the focused item relaxes, even when the
  // press lands on that very item and so no focus event follows. (On the same press the dock
  // stops showing for that focus.)
  const onPress = () => {
    keyboard = false;
    if (focused === null) return;
    focused = null;
    retarget();
  };

  const writeSizes = () => {
    for (const [id, item] of items) {
      const spring = springs.get(id);
      if (!spring) continue;
      item.style.width = `${spring.size}px`;
      item.style.height = `${spring.size}px`;
    }
  };

  // The badge lives inside the tile, so it hops along; the label is hidden by data-hop.
  const writeHop = (id: string, hop: HopState | undefined) => {
    const item = items.get(id);
    const tile = item?.querySelector<HTMLElement>(".dock-tile");
    if (!item || !tile || !layout) return;
    tile.style.transform = hop ? LIFT[layout.position](hop.lift) : "";
    item.toggleAttribute("data-hop", hop !== undefined);
  };

  const moving = () =>
    hops.size > 0 ||
    [...springs].some(([id, spring]) => !isSettled(spring, targets.get(id) ?? spring.size));

  const tick = (now: number) => {
    frame = null;
    if (!live || !layout) return;
    const dt = last === null ? FIRST_FRAME : Math.min(Math.max((now - last) / 1000, 0), LONGEST_FRAME);
    last = now;
    for (const [id, spring] of springs) {
      const target = targets.get(id) ?? spring.size;
      const next = stepSpring(spring, target, dt, layout.spring);
      springs.set(id, isSettled(next, target) ? { size: target, velocity: 0 } : next);
    }
    writeSizes();
    for (const [id, hop] of hops) {
      const next = stepHop(hop, dt);
      if (next.active) hops.set(id, next);
      else hops.delete(id);
      writeHop(id, next.active ? next : undefined);
    }
    if (moving()) frame = requestAnimationFrame(tick);
    else last = null;
  };

  const run = () => {
    if (live && frame === null && moving()) frame = requestAnimationFrame(tick);
  };

  // Targets from what drives the magnification: the pointer while it is over the bar, else the
  // item keyboard focus is on (as if the pointer rested on its rest centre), else nothing.
  // Left alone while a menu is open.
  const retarget = () => {
    if (!layout || held) return;
    const { centres, metrics } = layout;
    const at = pointer ?? (focused === null ? undefined : centres.get(focused));
    for (const [id, centre] of centres) {
      const size =
        at === undefined
          ? metrics.base
          : magnifiedSize(at - centre, metrics.base, metrics.peak, metrics.distance);
      targets.set(id, size);
    }
    if (reducedMotion()) {
      for (const [id, target] of targets) springs.set(id, { size: target, velocity: 0 });
      writeSizes();
    } else {
      run();
    }
  };

  return {
    // After every render: the items may have changed, and React writes an item's width and
    // height whenever base changes, so the current sizes go back on.
    layout(next: MotionLayout) {
      layout = next;
      items = new Map();
      for (const item of next.bar?.querySelectorAll<HTMLElement>(".dock-item") ?? []) {
        if (item.dataset.appId) items.set(item.dataset.appId, item);
      }
      for (const id of springs.keys()) {
        if (next.centres.has(id)) continue;
        springs.delete(id);
        targets.delete(id);
        hops.delete(id);
      }
      for (const id of next.centres.keys()) {
        if (!springs.has(id)) springs.set(id, { size: next.metrics.base, velocity: 0 });
      }
      writeSizes();
      retarget();
    },

    // The bar stays centred on its edge however its items grow, so its centre (measured here,
    // never per frame) and the rest length give where the rest positions start.
    pointerMove(x: number, y: number) {
      if (!layout?.bar) return;
      const rect = layout.bar.getBoundingClientRect();
      const vertical = layout.position === "left" || layout.position === "right";
      const centre = vertical ? (rect.top + rect.bottom) / 2 : (rect.left + rect.right) / 2;
      pointer = (vertical ? y : x) - (centre - layout.length / 2);
      retarget();
    },

    pointerLeave() {
      pointer = null;
      retarget();
    },

    // Focus moved onto an item. Only focus the keyboard put there magnifies it (as if the pointer
    // rested on it) and keeps a hidden dock shown: focus a pointer press put there must not keep
    // the item large, or the dock shown, once the pointer leaves. Returns whether the keyboard
    // put it there; focus with no input before it counts as the keyboard's, as for :focus-visible.
    focusItem(id: string): boolean {
      focused = keyboard ? id : null;
      retarget();
      return keyboard;
    },

    blur() {
      focused = null;
      retarget();
    },

    hold(on: boolean) {
      held = on;
      retarget();
    },

    // The click bounce, from the item's current spring size (never 0, which would make a hop
    // without gravity that never lands). A hop already in the air carries on.
    hop(id: string) {
      const spring = springs.get(id);
      if (!live || !spring || !(spring.size > 0) || hops.has(id) || reducedMotion()) return;
      const hop = startHop(spring.size);
      hops.set(id, hop);
      writeHop(id, hop);
      run();
    },

    start() {
      live = true;
      document.addEventListener("keydown", onKey, true);
      document.addEventListener("pointerdown", onPress, true);
      run();
    },

    stop() {
      live = false;
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPress, true);
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      last = null;
    },
  };
}

// What shows a hidden dock: the pointer along its edge or on it, keyboard focus on an item, an
// open menu.
type RevealReason = "edge" | "over" | "focus" | "menu";

// Whether a hidden dock shows: at once while any reason holds, and until HIDE_DELAY_MS after
// the last one ends. The dock raises and drops each reason; the setter never changes.
function useReveal(): [boolean, (reason: RevealReason, on: boolean) => void] {
  const [revealed, setRevealed] = useState(false);
  const state = useRef({
    reasons: new Set<RevealReason>(),
    shown: false,
    timer: undefined as ReturnType<typeof setTimeout> | undefined,
  });

  useEffect(() => {
    const s = state.current;
    return () => {
      clearTimeout(s.timer);
      s.timer = undefined;
    };
  }, []);

  const setReason = useCallback((reason: RevealReason, on: boolean) => {
    const s = state.current;
    if (s.reasons.has(reason) === on) return;
    if (on) s.reasons.add(reason);
    else s.reasons.delete(reason);
    if (s.reasons.size > 0) {
      clearTimeout(s.timer);
      s.timer = undefined;
      if (!s.shown) {
        s.shown = true;
        setRevealed(true);
      }
    } else if (s.shown && s.timer === undefined) {
      s.timer = setTimeout(() => {
        s.timer = undefined;
        s.shown = false;
        setRevealed(false);
      }, HIDE_DELAY_MS);
    }
  }, []);

  return [revealed, setReason];
}

export default function Dock() {
  const windows = useWindowStore((s) => s.windows);
  const openWindow = useWindowStore((s) => s.openWindow);
  const role = useAuthStore((s) => s.role);
  const settings = useSettingsStore((s) => s.dock);
  const alertCount = useActiveAlerts().length;

  const barRef = useRef<HTMLDivElement>(null);
  const press = useRef<Press | null>(null);
  // Set when a long press opens the menu, so the click that ends the press launches nothing.
  // That click clears it, and so does the menu closing when the browser sent no click.
  const swallowClick = useRef(false);
  const [menu, setMenu] = useState<OpenMenu | null>(null);
  const [stopId, setStopId] = useState<string | null>(null);
  const [motion] = useState(createDockMotion);
  const [viewport, setViewport] = useState(readViewport);
  const [revealed, setReveal] = useReveal();

  const entries = useMemo(
    () => buildDockEntries({ apps: APP_DEFINITIONS, role, windows, alertCount }),
    [role, windows, alertCount],
  );
  // Roving focus: the one Tab stop is the item focused last while it is on the bar, else the
  // first item.
  const appIds = entries.flatMap((entry) => (entry.kind === "app" ? [entry.app.id] : []));
  const tabStop = stopId !== null && appIds.includes(stopId) ? stopId : appIds[0];

  // Menu rows act on the windows they were built from, so they are rebuilt whenever the
  // windows change while the menu is open, and only then.
  const menuAppId = menu?.appId ?? null;
  const menuRows = useMemo(
    () => (menuAppId === null ? [] : buildAppMenu(menuAppId, windows, WINDOW_ACTIONS)),
    [menuAppId, windows],
  );

  useEffect(() => () => clearTimeout(press.current?.timer), []);

  const { position, theme } = settings;
  const vertical = position === "left" || position === "right";

  // Shrink to fit: the dock at its longest, its rest length plus the most its items grow together
  // (several swell at once around the pointer), must fit the viewport along the dock's side, less
  // EDGE at both ends (and the menubar for a side dock); otherwise every size scales. Every length
  // but a separator's 1px is in proportion to the scale, so the longest is worked out at scale 1.
  const full = dockMetrics(settings, 1);
  const fullRest = restLayout(entries, full);
  const needed =
    fullRest.length + peakSwell([...fullRest.centres.values()], full.base, full.peak, full.distance);
  const room = (vertical ? viewport.height - MENUBAR_HEIGHT : viewport.width) - 2 * DOCK_EDGE;
  const metrics = dockMetrics(settings, fitScale(needed, room));
  const rest = restLayout(entries, metrics);

  // Hidden while auto-hide is on or a maximised window is on screen, unless something reveals
  // it. A minimised window keeps its maximised flag until it is restored, but covers nothing.
  const hidden =
    (settings.autoHide || windows.some((w) => w.isMaximized && !w.isMinimized)) && !revealed;
  const revealDepth = DOCK_EDGE + metrics.thickness * REVEAL_DEPTH;

  useLayoutEffect(() => {
    motion.layout({
      bar: barRef.current,
      centres: rest.centres,
      length: rest.length,
      metrics,
      spring: DOCK_SPRINGS[settings.spring],
      position,
    });
  });

  useEffect(() => {
    motion.start();
    return () => motion.stop();
  }, [motion]);

  useEffect(() => {
    const onResize = () => setViewport(readViewport());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Keyboard focus keeps a hidden dock shown only until the pointer is used: a press anywhere
  // ends it, also one on the focused item itself, which fires no focus or blur event.
  useEffect(() => {
    const onPress = () => setReveal("focus", false);
    document.addEventListener("pointerdown", onPress, true);
    return () => document.removeEventListener("pointerdown", onPress, true);
  }, [setReveal]);

  // The pointer anywhere along the dock's edge reveals it.
  useEffect(() => {
    const onMove = (e: globalThis.PointerEvent) =>
      setReveal("edge", edgeDistance(position, e.clientX, e.clientY) <= revealDepth);
    window.addEventListener("pointermove", onMove);
    return () => window.removeEventListener("pointermove", onMove);
  }, [position, revealDepth, setReveal]);

  const itemButtons = () =>
    Array.from(barRef.current?.querySelectorAll<HTMLButtonElement>(".dock-item") ?? []);

  // Focuses the item at `index` along the bar, wrapping round both ends.
  const focusItem = (index: number) => {
    const buttons = itemButtons();
    buttons[(index + buttons.length) % buttons.length]?.focus();
  };

  // While a menu is open the dock stays shown and keeps its magnification, so the menu, which
  // stays where it opened, keeps pointing at its item.
  const openMenu = (appId: string, item: HTMLElement, initialFocus: OpenMenu["initialFocus"]) => {
    motion.hold(true);
    setReveal("menu", true);
    const { left, top, right, bottom } = item.getBoundingClientRect();
    setMenu({ appId, anchor: { left, top, right, bottom }, initialFocus });
  };

  // The menu reports why it closes and goes when this unmounts it. Escape and Tab hand focus
  // back to its item; the other ways out (a pick, a click elsewhere, blur, resize, scroll)
  // leave focus alone.
  const closeMenu = (reason: DockMenuCloseReason) => {
    const appId = menu?.appId;
    setMenu(null);
    swallowClick.current = false;
    if (reason === "escape" || reason === "tab") {
      itemButtons().find((button) => button.dataset.appId === appId)?.focus();
    }
    motion.hold(false);
    setReveal("menu", false);
  };

  // Drops a pending long press; true when there was one.
  const cancelPress = () => {
    if (!press.current) return false;
    clearTimeout(press.current.timer);
    press.current = null;
    return true;
  };

  const handlePointerDown = (e: PointerEvent<HTMLButtonElement>, appId: string) => {
    cancelPress();
    if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
    const item = e.currentTarget;
    press.current = {
      pointerId: e.pointerId,
      x: e.clientX,
      y: e.clientY,
      timer: setTimeout(() => {
        press.current = null;
        swallowClick.current = true;
        openMenu(appId, item, "none");
      }, LONG_PRESS_MS),
    };
  };

  const handlePointerMove = (e: PointerEvent<HTMLButtonElement>) => {
    const held = press.current;
    if (!held || e.pointerId !== held.pointerId) return;
    if (Math.hypot(e.clientX - held.x, e.clientY - held.y) > LONG_PRESS_SLOP) cancelPress();
  };

  const handleClick = (appId: string) => {
    if (swallowClick.current) {
      swallowClick.current = false;
      return;
    }
    openWindow(appId);
    if (settings.bounce) motion.hop(appId);
  };

  const handleContextMenu = (e: MouseEvent<HTMLButtonElement>, appId: string) => {
    e.preventDefault();
    // The browser's own long-press menu can come before the timer; it ends the press too.
    if (cancelPress()) swallowClick.current = true;
    openMenu(appId, e.currentTarget, "none");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLButtonElement>, appId: string) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const index = itemButtons().indexOf(e.currentTarget);
    const { key } = e;
    if (key === "ContextMenu" || (key === "F10" && e.shiftKey) || key === MENU_KEY[position]) {
      openMenu(appId, e.currentTarget, "first");
    } else if (key === (vertical ? "ArrowUp" : "ArrowLeft")) {
      focusItem(index - 1);
    } else if (key === (vertical ? "ArrowDown" : "ArrowRight")) {
      focusItem(index + 1);
    } else if (key === "Home") {
      focusItem(0);
    } else if (key === "End") {
      focusItem(-1);
    } else {
      return;
    }
    e.preventDefault();
  };

  // Geometry for the CSS. The bar's corner follows the tiles' plus its padding; once the pointer
  // is on the dock, the hover region reaches past the bar by one item's swell plus HOVER_REACH,
  // so moving across grown items keeps the swell.
  const style = {
    "--dock-base": `${metrics.base}px`,
    "--dock-thickness": `${metrics.thickness}px`,
    "--dock-pad": `${metrics.pad}px`,
    "--dock-gap": `${metrics.gap}px`,
    "--dock-radius": `${metrics.base * settings.roundness * 0.5 + metrics.pad}px`,
    "--dock-roundness": settings.roundness,
    "--dock-edge": `${DOCK_EDGE}px`,
    "--dock-hover-reach": `${metrics.peak - metrics.base + HOVER_REACH}px`,
    "--dock-accent": settings.accentColor,
    "--dock-badge": settings.badgeColor,
  } as CSSProperties;

  return (
    <>
      <div
        ref={barRef}
        className="dock"
        role="toolbar"
        aria-label="Dock"
        aria-orientation={vertical ? "vertical" : "horizontal"}
        data-position={position}
        data-theme={theme}
        data-bare={settings.tiles ? undefined : ""}
        data-hidden={hidden ? "" : undefined}
        style={style}
        onPointerMove={(e) => {
          setReveal("over", true);
          motion.pointerMove(e.clientX, e.clientY);
        }}
        onPointerLeave={() => {
          setReveal("over", false);
          motion.pointerLeave();
        }}
        onBlur={(e) => {
          if (e.currentTarget.contains(e.relatedTarget)) return;
          setReveal("focus", false);
          motion.blur();
        }}
      >
        <div className="dock-glass" aria-hidden="true" />
        {entries.map((entry) => {
          if (entry.kind === "separator") {
            return (
              <div
                key={entry.key}
                className="dock-divider"
                role="separator"
                aria-orientation={vertical ? "horizontal" : "vertical"}
              />
            );
          }
          const { app, open, badge } = entry;
          const Icon = ICON_MAP[app.icon] ?? FallbackIcon;
          return (
            <button
              key={entry.key}
              type="button"
              className="dock-item"
              aria-label={open ? `${app.title} (open)` : app.title}
              data-app-id={app.id}
              data-open={open ? "" : undefined}
              data-menu-open={menu?.appId === app.id ? "" : undefined}
              tabIndex={app.id === tabStop ? 0 : -1}
              style={{ width: metrics.base, height: metrics.base }}
              onClick={() => handleClick(app.id)}
              onContextMenu={(e) => handleContextMenu(e, app.id)}
              onKeyDown={(e) => handleKeyDown(e, app.id)}
              onFocus={() => {
                setStopId(app.id);
                setReveal("focus", motion.focusItem(app.id));
              }}
              onPointerDown={(e) => handlePointerDown(e, app.id)}
              onPointerMove={handlePointerMove}
              onPointerUp={cancelPress}
              onPointerCancel={cancelPress}
            >
              <span className="dock-tile" aria-hidden="true">
                <Icon className="dock-glyph" />
                {settings.showBadges && badge !== null && <span className="dock-count">{badge}</span>}
              </span>
              {settings.showLabels && (
                <span className="dock-tip" aria-hidden="true">
                  {app.title}
                </span>
              )}
              {settings.showIndicators && open && <span className="dock-pip" aria-hidden="true" />}
            </button>
          );
        })}
      </div>
      {menu && (
        <DockMenu
          key={menu.appId}
          rows={menuRows}
          anchor={menu.anchor}
          side={position}
          theme={theme}
          accentColor={settings.accentColor}
          onClose={closeMenu}
          initialFocus={menu.initialFocus}
        />
      )}
    </>
  );
}
