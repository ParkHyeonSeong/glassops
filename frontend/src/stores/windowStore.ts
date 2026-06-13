import { create } from "zustand";
import type { WindowState, AppDefinition, WindowBounds } from "../types";
import { useAuthStore } from "./authStore";

export const APP_DEFINITIONS: AppDefinition[] = [
  {
    id: "system-monitor",
    title: "System Monitor",
    icon: "BarChart3",
    defaultWidth: 680,
    defaultHeight: 480,
    minWidth: 400,
    minHeight: 300,
  },
  {
    id: "container-logs",
    title: "Container Logs",
    icon: "FileText",
    defaultWidth: 760,
    defaultHeight: 520,
    minWidth: 480,
    minHeight: 320,
    multiInstance: true,
    hiddenFromLauncher: true,
  },
  {
    id: "container-metrics",
    title: "Container Metrics",
    icon: "Activity",
    defaultWidth: 720,
    defaultHeight: 480,
    minWidth: 480,
    minHeight: 320,
    multiInstance: true,
    hiddenFromLauncher: true,
  },
  {
    id: "gpu-monitor",
    title: "GPU Monitor",
    icon: "Cpu",
    defaultWidth: 780,
    defaultHeight: 520,
    minWidth: 500,
    minHeight: 360,
  },
  {
    id: "docker",
    title: "Docker",
    icon: "Container",
    defaultWidth: 720,
    defaultHeight: 500,
    minWidth: 480,
    minHeight: 360,
    adminOnly: true,   // container env/mounts/networks + actions are admin-only (API-enforced)
  },
  {
    id: "network",
    title: "Network",
    icon: "Globe",
    defaultWidth: 700,
    defaultHeight: 460,
    minWidth: 420,
    minHeight: 300,
  },
  {
    id: "process",
    title: "Process Viewer",
    icon: "ListTree",
    defaultWidth: 680,
    defaultHeight: 480,
    minWidth: 480,
    minHeight: 300,
  },
  {
    id: "logs",
    title: "Logs",
    icon: "FileText",
    defaultWidth: 700,
    defaultHeight: 460,
    minWidth: 400,
    minHeight: 280,
  },
  {
    id: "terminal",
    title: "Terminal",
    icon: "TerminalSquare",
    defaultWidth: 680,
    defaultHeight: 420,
    minWidth: 400,
    minHeight: 260,
  },
  {
    id: "settings",
    title: "Settings",
    icon: "Settings",
    defaultWidth: 600,
    defaultHeight: 480,
    minWidth: 400,
    minHeight: 360,
  },
  {
    id: "users",
    title: "Users",
    icon: "Users",
    defaultWidth: 760,
    defaultHeight: 520,
    minWidth: 520,
    minHeight: 400,
    adminOnly: true,
  },
];

type WindowSize = Pick<WindowBounds, "width" | "height">;

interface WindowStore {
  windows: WindowState[];
  nextZIndex: number;
  // Last resized size per appId, restored on the next openWindow so users
  // don't have to redo their layout every time. Persists to localStorage.
  lastSizes: Record<string, WindowSize>;

  openWindow: (
    appId: string,
    options?: { params?: Record<string, string>; title?: string },
  ) => void;
  closeWindow: (windowId: string) => void;
  minimizeWindow: (windowId: string) => void;
  maximizeWindow: (windowId: string, prevBounds: WindowBounds) => void;
  restoreWindow: (windowId: string) => void;
  focusWindow: (windowId: string) => void;
  updateWindowPosition: (windowId: string, x: number, y: number) => void;
  updateWindowSize: (
    windowId: string,
    width: number,
    height: number
  ) => void;
  updateWindowOpacity: (windowId: string, opacity: number) => void;
  snapWindow: (windowId: string, side: "left" | "right") => void;
  closeFocusedWindow: () => void;
}

const LAST_SIZES_KEY = "glassops_window_sizes";

function loadLastSizes(): Record<string, WindowSize> {
  try {
    const raw = localStorage.getItem(LAST_SIZES_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    // Filter to entries with finite width/height numbers; tolerates user-edited
    // localStorage and accidental shape drift across releases.
    const out: Record<string, WindowSize> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (
        v && typeof v === "object" &&
        "width" in v && "height" in v &&
        Number.isFinite((v as WindowSize).width) &&
        Number.isFinite((v as WindowSize).height)
      ) {
        out[k] = { width: (v as WindowSize).width, height: (v as WindowSize).height };
      }
    }
    return out;
  } catch {
    return {};
  }
}

function saveLastSizes(sizes: Record<string, WindowSize>): void {
  try {
    localStorage.setItem(LAST_SIZES_KEY, JSON.stringify(sizes));
  } catch {
    // localStorage may be unavailable (private mode, quota). Silent failure is
    // fine — the in-memory copy still works for this session.
  }
}

// Clamp into [min, max]. When min > max (e.g. tiny viewport with a larger
// app minWidth), min wins — the window stays at minimum usable width even if
// it slightly overflows the viewport, which is preferable to a 0-sized window.
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

// Chrome reserved by the menubar (top) and dock (bottom). Keep in sync with
// the values in `snapWindow` and `Window.tsx`.
const MENUBAR_HEIGHT = 36;
const DOCK_HEIGHT = 72;

function paramsKey(params?: Record<string, string>): string {
  if (!params) return "";
  // Stable, collision-safe serialization. encodeURIComponent guards against
  // values containing `=`/`&` (current callers pass container names only, but
  // future callers may pass search queries or paths).
  return Object.keys(params)
    .sort()
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`)
    .join("&");
}

let windowCounter = 0;

export const useWindowStore = create<WindowStore>((set, get) => ({
  windows: [],
  nextZIndex: 1,
  lastSizes: loadLastSizes(),

  openWindow: (appId, options) => {
    const { windows, nextZIndex, lastSizes } = get();
    const params = options?.params;
    const app = APP_DEFINITIONS.find((a) => a.id === appId);
    if (!app) return;

    // Defense-in-depth: never open an admin-only app for a non-admin, even if the
    // call is forged (the dock already hides these, and the API enforces authz).
    // Reusing the adminOnly flag here is the single source of truth, so any future
    // admin-only app is covered automatically.
    if (app.adminOnly && useAuthStore.getState().role !== "admin") return;

    // For multi-instance apps we dedupe by (appId + params); single-instance
    // apps dedupe by appId alone (existing behavior).
    const sameInstance = (w: WindowState) =>
      w.appId === appId &&
      (app.multiInstance ? paramsKey(w.params) === paramsKey(params) : true);

    const existing = windows.find((w) => sameInstance(w) && !w.isMinimized);
    if (existing) {
      get().focusWindow(existing.id);
      return;
    }

    const minimized = windows.find((w) => sameInstance(w) && w.isMinimized);
    if (minimized) {
      get().restoreWindow(minimized.id);
      return;
    }

    const offset = (windowCounter % 8) * 30;
    windowCounter++;

    // Restore the user's last size for this appId; clamp into a sane range so
    // a stored size from a larger display doesn't push the window off-screen.
    // Height excludes menubar + dock so a restored window never overlaps them.
    const remembered = lastSizes[appId];
    const viewportW = globalThis.innerWidth || app.defaultWidth;
    const viewportH = (globalThis.innerHeight || app.defaultHeight) - MENUBAR_HEIGHT - DOCK_HEIGHT;
    const width = clamp(remembered?.width ?? app.defaultWidth, app.minWidth, viewportW);
    const height = clamp(remembered?.height ?? app.defaultHeight, app.minHeight, viewportH);

    const newWindow: WindowState = {
      id: `window-${crypto.randomUUID()}`,
      appId: app.id,
      title: options?.title ?? app.title,
      x: 100 + offset,
      y: 50 + offset,
      width,
      height,
      minWidth: app.minWidth,
      minHeight: app.minHeight,
      isMinimized: false,
      isMaximized: false,
      zIndex: nextZIndex,
      opacity: 1,
      preMaximizeBounds: null,
      params,
    };

    set({
      windows: [...windows, newWindow],
      nextZIndex: nextZIndex + 1,
    });
  },

  closeWindow: (windowId: string) => {
    set((state) => ({
      windows: state.windows.filter((w) => w.id !== windowId),
    }));
  },

  minimizeWindow: (windowId: string) => {
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, isMinimized: true } : w
      ),
    }));
  },

  maximizeWindow: (windowId: string, prevBounds: WindowBounds) => {
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId
          ? { ...w, isMaximized: true, preMaximizeBounds: prevBounds }
          : w
      ),
    }));
  },

  restoreWindow: (windowId: string) => {
    const { nextZIndex } = get();
    set((state) => ({
      windows: state.windows.map((w) => {
        if (w.id !== windowId) return w;
        const bounds = w.preMaximizeBounds;
        return {
          ...w,
          isMinimized: false,
          isMaximized: false,
          zIndex: nextZIndex,
          preMaximizeBounds: null,
          ...(bounds ?? {}),
        };
      }),
      nextZIndex: nextZIndex + 1,
    }));
  },

  focusWindow: (windowId: string) => {
    const { nextZIndex } = get();
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, zIndex: nextZIndex } : w
      ),
      nextZIndex: nextZIndex + 1,
    }));
  },

  updateWindowPosition: (windowId: string, x: number, y: number) => {
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, x, y } : w
      ),
    }));
  },

  updateWindowSize: (windowId: string, width: number, height: number) => {
    set((state) => {
      const win = state.windows.find((w) => w.id === windowId);
      if (!win) return state;
      // Maximized windows fill the viewport — that size isn't a user preference,
      // so don't overwrite the saved one. Restore/snap-out will return to the
      // pre-maximize size anyway.
      const shouldRemember = !win.isMaximized;
      const lastSizes = shouldRemember
        ? { ...state.lastSizes, [win.appId]: { width, height } }
        : state.lastSizes;
      if (shouldRemember) saveLastSizes(lastSizes);
      return {
        windows: state.windows.map((w) =>
          w.id === windowId ? { ...w, width, height } : w
        ),
        lastSizes,
      };
    });
  },

  updateWindowOpacity: (windowId: string, opacity: number) => {
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, opacity: Math.max(0.3, Math.min(1, opacity)) } : w
      ),
    }));
  },

  snapWindow: (windowId: string, side: "left" | "right") => {
    const { nextZIndex } = get();
    const screenW = globalThis.innerWidth;
    const menuH = 36;
    const dockH = 72;
    const availH = globalThis.innerHeight - menuH - dockH;

    set((state) => ({
      windows: state.windows.map((w) => {
        if (w.id !== windowId) return w;
        return {
          ...w,
          x: side === "left" ? 0 : screenW / 2,
          y: 0,
          width: screenW / 2,
          height: availH,
          isMaximized: false,
          preMaximizeBounds: w.preMaximizeBounds ?? { x: w.x, y: w.y, width: w.width, height: w.height },
          zIndex: nextZIndex,
        };
      }),
      nextZIndex: nextZIndex + 1,
    }));
  },

  closeFocusedWindow: () => {
    const { windows } = get();
    const visible = windows.filter((w) => !w.isMinimized);
    if (visible.length === 0) return;
    const topWindow = visible.reduce((a, b) => (a.zIndex > b.zIndex ? a : b));
    get().closeWindow(topWindow.id);
  },
}));
