import { AppWindow } from "lucide-react";
import { describe, expect, it } from "vitest";
import { APP_DEFINITIONS } from "../../../stores/windowStore";
import type { WindowState } from "../../../types";
import {
  appGroup, buildAppMenu, buildDockEntries, formatBadge, groupWindows, type DockMenuActions, type DockMenuRow,
} from "../dockModel";

let seq = 0;
function win(appId: string, extra: Partial<WindowState> = {}): WindowState {
  seq += 1;
  return { id: `w${seq}`, appId, title: appId, x: 0, y: 0, width: 400, height: 300, minWidth: 100,
    minHeight: 100, isMinimized: false, isMaximized: false, zIndex: seq, opacity: 1, preMaximizeBounds: null, ...extra };
}
const ids = (entries: ReturnType<typeof buildDockEntries>) => entries.map((e) => (e.kind === "separator" ? "|" : e.app.id));
const labels = (rows: DockMenuRow[]) => rows.map((r) => (r.kind === "separator" ? "—" : r.label));
const item = (rows: DockMenuRow[], label: string) => {
  const row = rows.find((r) => r.kind === "item" && r.label === label);
  if (!row || row.kind !== "item") throw new Error(`no row ${label}`);
  return row;
};
function recorder(): { calls: string[]; actions: DockMenuActions } {
  const calls: string[] = [];
  const log = (name: string) => (arg: string) => { calls.push(`${name}:${arg}`); };
  return { calls, actions: { focusWindow: log("focus"), restoreWindow: log("restore"), minimizeWindow: log("minimize"),
    closeWindow: log("close"), openWindow: log("open") } };
}

describe("buildDockEntries", () => {
  it("lists launcher apps for an admin with one separator before Settings", () => {
    expect(ids(buildDockEntries({ apps: APP_DEFINITIONS, role: "admin", windows: [], alertCount: 0 }))).toEqual(
      ["system-monitor", "gpu-monitor", "docker", "network", "process", "logs", "terminal", "|", "settings", "users"]);
  });
  it("hides admin-only apps from other roles and keeps one inner separator", () => {
    const out = ids(buildDockEntries({ apps: APP_DEFINITIONS, role: "viewer", windows: [], alertCount: 0 }));
    expect(out).toEqual(["system-monitor", "gpu-monitor", "network", "process", "logs", "terminal", "|", "settings"]);
    expect(out[0]).not.toBe("|");
    expect(out.at(-1)).not.toBe("|");
  });
  it("marks an app open when any window of its group exists, minimised included", () => {
    const entries = buildDockEntries({ apps: APP_DEFINITIONS, role: "admin",
      windows: [win("container-logs"), win("terminal", { isMinimized: true })], alertCount: 0 });
    expect(entries.flatMap((e) => (e.kind === "app" && e.open ? [e.app.id] : []))).toEqual(["docker", "terminal"]);
  });
  it("puts the active-alert count on System Monitor only", () => {
    const entries = buildDockEntries({ apps: APP_DEFINITIONS, role: "admin", windows: [], alertCount: 3 });
    expect(entries.flatMap((e) => (e.kind === "app" && e.badge ? [[e.app.id, e.badge]] : []))).toEqual([["system-monitor", "3"]]);
  });
});

describe("formatBadge / appGroup", () => {
  it("hides zero and caps at 99+", () => {
    expect([0, 7, 99, 100].map(formatBadge)).toEqual([null, "7", "99", "99+"]);
  });
  it("groups the container windows under Docker", () => {
    expect(appGroup("docker")).toEqual(["docker", "container-logs", "container-metrics"]);
    expect(appGroup("terminal")).toEqual(["terminal"]);
  });
});

describe("buildAppMenu", () => {
  it("offers Open with disabled window actions for a closed app", () => {
    const rows = buildAppMenu("network", [], recorder().actions);
    expect(labels(rows)).toEqual(["Show All Windows", "Hide", "—", "Open"]);
    expect(item(rows, "Show All Windows").disabled).toBe(true);
    expect(item(rows, "Hide").disabled).toBe(true);
  });
  it("lists Docker's container windows and checks the front-most visible one", () => {
    const rows = buildAppMenu("docker", [
      win("docker", { title: "Docker", zIndex: 5 }), win("terminal"),
      win("container-logs", { title: "Logs: nginx", zIndex: 9 }),
      win("container-metrics", { title: "Metrics: redis", zIndex: 12, isMinimized: true }),
    ], recorder().actions);
    expect(labels(rows)).toEqual(["Docker", "Logs: nginx", "Metrics: redis", "—", "Show All Windows", "Hide", "—", "Quit"]);
    expect(rows.flatMap((r) => (r.kind === "item" && r.checked ? [r.label] : []))).toEqual(["Logs: nginx"]);
    expect(item(rows, "Docker").checked).toBe(false);
    expect(item(rows, "Docker").icon).toBeDefined();
  });
  it("Quit closes every window in the group and nothing else", () => {
    const { calls, actions } = recorder();
    const d = win("docker"); const l = win("container-logs"); const t = win("terminal");
    item(buildAppMenu("docker", [d, t, l], actions), "Quit").onSelect?.();
    expect(calls).toEqual([`close:${d.id}`, `close:${l.id}`]);
  });
  it("Hide minimises visible windows and is disabled when all are minimised", () => {
    const { calls, actions } = recorder();
    const a = win("logs"); const b = win("logs", { isMinimized: true });
    item(buildAppMenu("logs", [a, b], actions), "Hide").onSelect?.();
    expect(calls).toEqual([`minimize:${a.id}`]);
    expect(item(buildAppMenu("logs", [b], actions), "Hide").disabled).toBe(true);
  });
  it("Show All Windows restores minimised windows and focuses the rest in z-order", () => {
    const { calls, actions } = recorder();
    const a = win("logs", { zIndex: 3 }); const b = win("logs", { zIndex: 1, isMinimized: true }); const c = win("logs", { zIndex: 5 });
    item(buildAppMenu("logs", [a, b, c], actions), "Show All Windows").onSelect?.();
    expect(calls).toEqual([`restore:${b.id}`, `focus:${a.id}`, `focus:${c.id}`]);
  });
  it("a window row restores a minimised window and focuses a visible one", () => {
    const { calls, actions } = recorder();
    const a = win("logs", { title: "Logs A" }); const b = win("logs", { title: "Logs B", isMinimized: true });
    const rows = buildAppMenu("logs", [a, b], actions);
    item(rows, "Logs A").onSelect?.();
    item(rows, "Logs B").onSelect?.();
    expect(calls).toEqual([`focus:${a.id}`, `restore:${b.id}`]);
  });
  it("Open launches the app", () => {
    const { calls, actions } = recorder();
    item(buildAppMenu("network", [], actions), "Open").onSelect?.();
    expect(calls).toEqual(["open:network"]);
  });
});

// The finer rules: where the separator goes, entry keys, badge text at the edges, store order,
// row icons and disabled states, the group-wide rows' order, and menus that look only at the
// app's own windows when other apps have windows too.
const pick = (...appIds: string[]) => APP_DEFINITIONS.filter((a) => appIds.includes(a.id));
const disabledLabels = (rows: DockMenuRow[]) => rows.flatMap((r) => (r.kind === "item" && r.disabled ? [r.label] : []));

describe("buildDockEntries: separator, keys and badge text", () => {
  it("puts the separator before Settings only when an app precedes it", () => {
    const entries = (appIds: string[], role: string | null) =>
      ids(buildDockEntries({ apps: pick(...appIds), role, windows: [], alertCount: 0 }));
    expect(entries(["system-monitor", "settings"], "viewer")).toEqual(["system-monitor", "|", "settings"]);
    expect(entries(["docker", "settings"], "admin")).toEqual(["docker", "|", "settings"]);
    expect(entries(["settings"], "viewer")).toEqual(["settings"]);
    // Docker comes first in the list but is hidden from these roles, so nothing precedes Settings.
    expect(entries(["docker", "settings"], "viewer")).toEqual(["settings"]);
    expect(entries(["docker", "settings"], null)).toEqual(["settings"]);
  });
  it("gives every entry its own key", () => {
    const entries = buildDockEntries({ apps: APP_DEFINITIONS, role: "admin", windows: [], alertCount: 0 });
    const keys = entries.map((e) => e.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
  it("formats the System Monitor badge: none at zero, 99+ above 99", () => {
    const badges = (alertCount: number) =>
      buildDockEntries({ apps: APP_DEFINITIONS, role: "admin", windows: [], alertCount })
        .flatMap((e) => (e.kind === "app" && e.badge ? [e.badge] : []));
    expect(badges(0)).toEqual([]);
    expect(badges(250)).toEqual(["99+"]);
  });
});

describe("formatBadge: one alert and non-counts", () => {
  it("shows a single alert and hides anything that is not a positive count", () => {
    expect(formatBadge(1)).toBe("1");
    expect([-1, Number.NaN].map(formatBadge)).toEqual([null, null]);
  });
});

describe("groupWindows", () => {
  it("keeps store order, not stacking order, for the group and for the menu's window rows", () => {
    const logs = win("container-logs", { title: "Logs", zIndex: 4 });
    const docker = win("docker", { title: "Docker", zIndex: 9 });
    const metrics = win("container-metrics", { title: "Metrics", zIndex: 1 });
    const all = [logs, win("terminal", { zIndex: 2 }), docker, metrics];
    expect(groupWindows("docker", all).map((w) => w.id)).toEqual([logs.id, docker.id, metrics.id]);
    expect(labels(buildAppMenu("docker", all, recorder().actions)).slice(0, 3)).toEqual(["Logs", "Docker", "Metrics"]);
  });
});

describe("buildAppMenu: row icons, disabled rows and group actions", () => {
  it("uses the lucide AppWindow icon on window rows and leaves the action rows plain", () => {
    const running = buildAppMenu("docker", [win("docker", { title: "Docker" })], recorder().actions);
    expect(item(running, "Docker").icon).toBe(AppWindow);
    const plain = (rows: DockMenuRow[], label: string) => {
      const { checked, icon, shortcut, items } = item(rows, label);
      return [checked, icon, shortcut, items];
    };
    const none = [undefined, undefined, undefined, undefined];
    for (const label of ["Show All Windows", "Hide", "Quit"]) expect(plain(running, label)).toEqual(none);
    expect(plain(buildAppMenu("network", [], recorder().actions), "Open")).toEqual(none);
  });
  it("enables every row of a running app and, for a closed one, disables only the window actions", () => {
    const running = buildAppMenu("docker", [win("docker", { title: "Docker" })], recorder().actions);
    expect(disabledLabels(running)).toEqual([]);
    expect(disabledLabels(buildAppMenu("network", [], recorder().actions))).toEqual(["Show All Windows", "Hide"]);
  });
  it("keeps Show All Windows and Quit, not Open, when every window is minimized", () => {
    const { calls, actions } = recorder();
    const a = win("logs", { zIndex: 2, isMinimized: true }); const b = win("logs", { zIndex: 1, isMinimized: true });
    const rows = buildAppMenu("logs", [a, b], actions);
    expect(labels(rows).slice(-4)).toEqual(["Show All Windows", "Hide", "—", "Quit"]);
    expect(item(rows, "Show All Windows").disabled).toBeFalsy();
    item(rows, "Show All Windows").onSelect?.();
    item(rows, "Quit").onSelect?.();
    expect(calls).toEqual([`restore:${b.id}`, `restore:${a.id}`, `close:${a.id}`, `close:${b.id}`]);
  });
  it("applies Hide and Show All Windows to Docker's container windows too", () => {
    const d = win("docker", { zIndex: 3 }); const l = win("container-logs", { zIndex: 1, isMinimized: true });
    const m = win("container-metrics", { zIndex: 2 });
    const windows = [d, l, m, win("terminal")];
    const hide = recorder();
    item(buildAppMenu("docker", windows, hide.actions), "Hide").onSelect?.();
    expect(hide.calls.sort()).toEqual([`minimize:${d.id}`, `minimize:${m.id}`].sort());
    const showAll = recorder();
    item(buildAppMenu("docker", windows, showAll.actions), "Show All Windows").onSelect?.();
    expect(showAll.calls).toEqual([`restore:${l.id}`, `focus:${m.id}`, `focus:${d.id}`]);
  });
  it("Show All Windows keeps the relative order when a minimized window sits between visible ones", () => {
    const { calls, actions } = recorder();
    const a = win("logs", { zIndex: 1 }); const b = win("logs", { zIndex: 3, isMinimized: true }); const c = win("logs", { zIndex: 5 });
    item(buildAppMenu("logs", [c, b, a], actions), "Show All Windows").onSelect?.();
    expect(calls).toEqual([`focus:${a.id}`, `restore:${b.id}`, `focus:${c.id}`]);
  });
});

describe("buildAppMenu: only the app's own windows count", () => {
  it("a closed app still gets Open and disabled window actions while other apps have windows", () => {
    const rows = buildAppMenu("network", [win("terminal"), win("logs")], recorder().actions);
    expect(labels(rows)).toEqual(["Show All Windows", "Hide", "—", "Open"]);
    expect(disabledLabels(rows)).toEqual(["Show All Windows", "Hide"]);
  });
  it("Hide stays disabled when the app's windows are all minimized and another app's window is visible", () => {
    const rows = buildAppMenu("logs", [win("logs", { isMinimized: true }), win("terminal")], recorder().actions);
    expect(item(rows, "Hide").disabled).toBe(true);
  });
  it("checks the app's own front-most window even when another app's window is on top", () => {
    const rows = buildAppMenu("logs", [win("logs", { title: "Mine", zIndex: 2 }), win("terminal", { zIndex: 50 })], recorder().actions);
    expect(item(rows, "Mine").checked).toBe(true);
  });
});
