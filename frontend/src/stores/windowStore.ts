import { create } from "zustand";
import type { WindowState, AppDefinition, WindowBounds } from "../types";

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
    id: "docker",
    title: "Docker",
    icon: "Container",
    defaultWidth: 720,
    defaultHeight: 500,
    minWidth: 480,
    minHeight: 360,
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
];

interface WindowStore {
  windows: WindowState[];
  nextZIndex: number;

  openWindow: (appId: string) => void;
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
  closeFocusedWindow: () => void;
}

let windowCounter = 0;

export const useWindowStore = create<WindowStore>((set, get) => ({
  windows: [],
  nextZIndex: 1,

  openWindow: (appId: string) => {
    const { windows, nextZIndex } = get();
    const existing = windows.find(
      (w) => w.appId === appId && !w.isMinimized
    );
    if (existing) {
      get().focusWindow(existing.id);
      return;
    }

    const minimized = windows.find(
      (w) => w.appId === appId && w.isMinimized
    );
    if (minimized) {
      get().restoreWindow(minimized.id);
      return;
    }

    const app = APP_DEFINITIONS.find((a) => a.id === appId);
    if (!app) return;

    const offset = (windowCounter % 8) * 30;
    windowCounter++;

    const newWindow: WindowState = {
      id: `window-${crypto.randomUUID()}`,
      appId: app.id,
      title: app.title,
      x: 100 + offset,
      y: 50 + offset,
      width: app.defaultWidth,
      height: app.defaultHeight,
      minWidth: app.minWidth,
      minHeight: app.minHeight,
      isMinimized: false,
      isMaximized: false,
      zIndex: nextZIndex,
      opacity: 1,
      preMaximizeBounds: null,
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
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, width, height } : w
      ),
    }));
  },

  updateWindowOpacity: (windowId: string, opacity: number) => {
    set((state) => ({
      windows: state.windows.map((w) =>
        w.id === windowId ? { ...w, opacity: Math.max(0.3, Math.min(1, opacity)) } : w
      ),
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
