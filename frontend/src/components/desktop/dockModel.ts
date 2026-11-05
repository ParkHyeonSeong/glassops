import type { ComponentType } from "react";
import { AppWindow } from "lucide-react";
import type { AppDefinition, WindowState } from "../../types";

// What the dock shows and what each app's context menu offers, as plain data. Pure: no
// store and no React rendering. Callers pass in the apps, the role, the windows and the
// actions to run, so the dock component and its tests drive it the same way.

// One slot on the bar: an app, or the divider that sits before Settings. `key` is unique
// within the list, for React.
export type DockEntry =
  | { kind: "separator"; key: string }
  | { kind: "app"; key: string; app: AppDefinition; open: boolean; badge: string | null };

// One row of an app's context menu. A `checked` flag, true or false, makes the row
// checkable; leave it out for a plain row. `items` makes the row open a submenu.
export type DockMenuRow =
  | { kind: "separator" }
  | {
      kind: "item";
      label: string;
      onSelect?: () => void;
      disabled?: boolean;
      checked?: boolean;
      icon?: ComponentType<{ size?: number }>;
      shortcut?: string;
      items?: DockMenuRow[];
    };

// The window store's actions that menu rows call; passed in so this module never imports it.
export interface DockMenuActions {
  focusWindow(id: string): void;
  restoreWindow(id: string): void;
  minimizeWindow(id: string): void;
  closeWindow(id: string): void;
  openWindow(appId: string): void;
}

// Which window appIds belong to which dock app. Container logs and metrics have no dock icon
// of their own, so they belong to Docker; every other app is just itself.
const APP_GROUPS = new Map<string, readonly string[]>([
  ["docker", ["docker", "container-logs", "container-metrics"]],
]);

const BADGE_APP_ID = "system-monitor";
const SETTINGS_APP_ID = "settings";

// Every appId whose windows belong to this app, the app's own included.
export function appGroup(appId: string): readonly string[] {
  return APP_GROUPS.get(appId) ?? [appId];
}

// The windows of an app's group, in store order.
export function groupWindows(appId: string, windows: WindowState[]): WindowState[] {
  const group = appGroup(appId);
  return windows.filter((w) => group.includes(w.appId));
}

// Badge text for a count of active alerts: none unless the count is positive (so none for
// NaN either), "99+" above 99.
export function formatBadge(count: number): string | null {
  if (!(count > 0)) return null;
  return count > 99 ? "99+" : String(count);
}

// The bar's slots: the launcher apps this role may see, in definition order, with one
// separator right before Settings once an app precedes it. An app is open while any
// window of its group exists, minimized or not; only System Monitor carries the badge.
export function buildDockEntries(input: {
  apps: AppDefinition[];
  role: string | null;
  windows: WindowState[];
  alertCount: number;
}): DockEntry[] {
  const { apps, role, windows, alertCount } = input;
  const openAppIds = new Set(windows.map((w) => w.appId));
  const launchers = apps.filter(
    (app) => !app.hiddenFromLauncher && (!app.adminOnly || role === "admin"),
  );

  const entries: DockEntry[] = [];
  for (const app of launchers) {
    if (app.id === SETTINGS_APP_ID && entries.length > 0) {
      entries.push({ kind: "separator", key: "separator" });
    }
    entries.push({
      kind: "app",
      key: app.id,
      app,
      open: appGroup(app.id).some((id) => openAppIds.has(id)),
      badge: app.id === BADGE_APP_ID ? formatBadge(alertCount) : null,
    });
  }
  return entries;
}

// Bring a window up: restore it if it is minimized, focus it otherwise.
function reveal(win: WindowState, actions: DockMenuActions): void {
  if (win.isMinimized) actions.restoreWindow(win.id);
  else actions.focusWindow(win.id);
}

// An app's context menu: one row per window of its group, then the group-wide actions.
// The rows act on the windows as they are when this is called, so build the menu again
// after the windows change.
export function buildAppMenu(
  appId: string,
  windows: WindowState[],
  actions: DockMenuActions,
): DockMenuRow[] {
  const group = groupWindows(appId, windows);
  const visible = group.filter((w) => !w.isMinimized);
  // The checked row is the window on top: the highest zIndex among the visible ones.
  const front = visible.reduce<WindowState | undefined>(
    (top, w) => (top && top.zIndex > w.zIndex ? top : w),
    undefined,
  );

  const rows: DockMenuRow[] = group.map(
    (w): DockMenuRow => ({
      kind: "item",
      label: w.title,
      icon: AppWindow,
      checked: w === front,
      onSelect: () => reveal(w, actions),
    }),
  );
  if (group.length > 0) rows.push({ kind: "separator" });

  rows.push(
    {
      kind: "item",
      label: "Show All Windows",
      disabled: group.length === 0,
      onSelect: () => {
        // Back to front, so each window lands above the one before and the group keeps
        // its stacking order.
        const backToFront = [...group].sort((a, b) => a.zIndex - b.zIndex);
        for (const w of backToFront) reveal(w, actions);
      },
    },
    {
      kind: "item",
      label: "Hide",
      disabled: visible.length === 0,
      onSelect: () => {
        for (const w of visible) actions.minimizeWindow(w.id);
      },
    },
    { kind: "separator" },
  );

  if (group.length > 0) {
    rows.push({
      kind: "item",
      label: "Quit",
      onSelect: () => {
        for (const w of group) actions.closeWindow(w.id);
      },
    });
  } else {
    rows.push({ kind: "item", label: "Open", onSelect: () => actions.openWindow(appId) });
  }
  return rows;
}
