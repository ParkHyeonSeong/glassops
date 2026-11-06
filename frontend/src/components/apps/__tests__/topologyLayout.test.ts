import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import {
  GROUP_GAP, LAYOUT_KEY, NODE_GAP, NODE_H, NODE_W, PAD_BOTTOM, PAD_X, ROW_GAP,
  arrangeNodes, defaultLayout, groupRect, loadLineToggles, loadSavedPositions,
  saveLineToggles, savePositions, snapToGrid, tabWidth, type Rect,
} from "../topologyLayout";
import type { GroupKind, NodeKind, TopoGroup, TopoNode, TopologyGraph } from "../topologyModel";

type G = Pick<TopologyGraph, "nodes" | "groups">;

function node(key: string, kind: NodeKind, groupId: string): TopoNode {
  return { key, kind, groupId, name: key, sub: "", persist: kind !== "proc", tip: { title: key, rows: [] } };
}
function group(id: string, kind: GroupKind, row: 0 | 1 | 2, underGpu?: boolean): TopoGroup {
  return { id, kind, label: id, row, ...(underGpu ? { underGpu } : {}) };
}
// hw 4, gpu 4, compose:shop 4, compose:llm 2, solo 2, pm2 3, other 1
function reference(): G {
  return {
    groups: [
      group("hw", "host", 0), group("gpu", "lane", 0),
      group("compose:shop", "compose", 1), group("compose:llm", "compose", 1), group("solo", "solo", 1),
      group("pm2", "lane", 2), group("other", "lane", 2, true),
    ],
    nodes: [
      node("hw:cpu", "cpu", "hw"), node("hw:mem", "mem", "hw"), node("hw:disk", "disk", "hw"), node("hw:net", "net", "hw"),
      ...Array.from({ length: 4 }, (_, i) => node(`gpu:GPU-${i}`, "gpu", "gpu")),
      node("ct:api", "container", "compose:shop"), node("ct:web", "container", "compose:shop"),
      node("ct:db", "container", "compose:shop"), node("ct:cache", "container", "compose:shop"),
      node("ct:llm1", "container", "compose:llm"), node("ct:llm2", "container", "compose:llm"),
      node("ct:s1", "container", "solo"), node("ct:s2", "container", "solo"),
      node("pm2:/h/.pm2:a", "pm2", "pm2"), node("pm2:/h/.pm2:b", "pm2", "pm2"), node("pm2:/h/.pm2:c", "pm2", "pm2"),
      node("proc:900", "proc", "other"),
    ],
  };
}

// 12 compose groups, 40 containers, 30 procs
function busy(): G {
  const groups: TopoGroup[] = [group("hw", "host", 0)];
  const nodes: TopoNode[] = [node("hw:cpu", "cpu", "hw"), node("hw:mem", "mem", "hw")];
  let c = 0;
  for (let i = 0; i < 12; i++) {
    const id = `compose:g${i}`;
    groups.push(group(id, "compose", 1));
    const count = i < 4 ? 4 : 3; // 16 + 24 = 40
    for (let j = 0; j < count; j++) nodes.push(node(`ct:${c++}`, "container", id));
  }
  groups.push(group("other", "lane", 2));
  for (let i = 0; i < 30; i++) nodes.push(node(`proc:${i}`, "proc", "other"));
  return { groups, nodes };
}

function withNewcomers(): G {
  return {
    groups: [group("compose:old", "compose", 1), group("compose:new", "compose", 1), group("other", "lane", 2)],
    nodes: [
      node("ct:a", "container", "compose:old"), node("ct:b", "container", "compose:old"),
      node("ct:n1", "container", "compose:new"), node("ct:n2", "container", "compose:new"),
      node("proc:1", "proc", "other"),
    ],
  };
}

function anyOverlap(boxes: Rect[]): boolean {
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) return true;
    }
  }
  return false;
}

describe("group width follows the tab", () => {
  const solo = (label: string, ids: string[]): G => ({
    groups: ids.map((id) => ({ id, kind: "compose" as const, label, tag: "compose", row: 1 as const })),
    nodes: ids.map((id) => node(`ct:${id}`, "container", id)),
  });

  it("estimates the tab width", () => {
    expect(tabWidth({ kind: "compose", label: "monitoring", tag: "compose" })).toBe(157);
    expect(tabWidth({ kind: "lane", label: "Hardware" })).toBe(70);
  });

  it("never makes a group narrower than its tab", () => {
    const g = solo("vector-search", ["a"]);
    const r = groupRect("a", g, defaultLayout(g, 1072))!;
    expect(r.w).toBe(12 + tabWidth(g.groups[0]) + 12);
    expect(r.w).toBeGreaterThan(116);
  });

  it("does not widen a group whose tab is unknown to the graph", () => {
    const g = solo("vector-search", ["a"]);
    expect(groupRect("a", { nodes: g.nodes, groups: [] }, defaultLayout(g, 1072))!.w).toBe(116);
  });

  it("starts the next group after the widened box", () => {
    const g = solo("vector-search", ["a", "b"]);
    const p = defaultLayout(g, 1072);
    const [a, b] = g.groups.map((x) => groupRect(x.id, g, p)!);
    expect(b.x).toBeGreaterThanOrEqual(a.x + a.w + GROUP_GAP);
    expect(anyOverlap([a, b])).toBe(false);
  });

  it("wraps on the widened width", () => {
    const g = solo("vector-search", ["a", "b"]);
    const w = 12 + tabWidth(g.groups[0]) + 12;
    const p = defaultLayout(g, 2 * w + GROUP_GAP - 1);
    expect(groupRect("b", g, p)!.x).toBe(0);
    expect(groupRect("b", g, p)!.y).toBeGreaterThan(0);
  });

  it("leaves wide groups alone", () => {
    const g = reference();
    g.groups.find((x) => x.id === "compose:llm")!.tag = "compose";
    const p = defaultLayout(g, 1072);
    expect(groupRect("hw", g, p)!.w).toBe(422);
    expect(groupRect("compose:llm", g, p)!.w).toBe(218);
  });

  it("places a newcomer below everything using the widened boxes", () => {
    const g = solo("vector-search", ["a", "b"]);
    g.groups.push(group("other", "lane", 2));
    g.nodes.push(node("ct:n", "container", "other"));
    const p = arrangeNodes(g, { "ct:a": { x: 0, y: 0 }, "ct:b": { x: 300, y: 0 } }, 1072);
    const boxes = g.groups.map((x) => groupRect(x.id, g, p)!);
    expect(anyOverlap(boxes)).toBe(false);
    expect(boxes[2].y).toBeGreaterThanOrEqual(boxes[0].y + boxes[0].h + ROW_GAP);
  });

  it("judges an unplaced group's slot by its widened box", () => {
    // 'n' is never saved and its default slot is x 0..126 once widened (116 before); a placed box starting at
    // x 120 overlaps only the widened slot, so 'n' has to move below it.
    const g: G = {
      groups: [
        { id: "n", kind: "lane", label: "vector-search", row: 0 },
        { id: "a", kind: "lane", label: "a", row: 0 },
      ],
      nodes: [node("proc:1", "proc", "n"), node("ct:a", "container", "a")],
    };
    const p = arrangeNodes(g, { "ct:a": { x: 130, y: 0 } }, 1072);
    const [rn, ra] = g.groups.map((x) => groupRect(x.id, g, p)!);
    expect(anyOverlap([rn, ra])).toBe(false);
    expect(rn.y).toBeGreaterThanOrEqual(ra.y + ra.h + ROW_GAP);
  });
});

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("topologyLayout", () => {
  it("lays out the reference graph", () => {
    const p = defaultLayout(reference(), 1072);
    expect(p["hw:cpu"]).toEqual({ x: 10, y: 18 });
    expect(p["hw:net"]).toEqual({ x: 316, y: 18 });
    expect(p["gpu:GPU-0"]).toEqual({ x: 448, y: 18 });
    expect(p["ct:api"]).toEqual({ x: 10, y: 202 });
    expect(p["pm2:/h/.pm2:a"]).toEqual({ x: 10, y: 386 });
    // Other processes: right-aligned under the GPU group (its box ends where the GPU box ends).
    expect(p["proc:900"]).toEqual({ x: 754, y: 386 });
    const right = (id: string) => { const r = groupRect(id, reference(), p)!; return r.x + r.w; };
    expect(right("other")).toBe(right("gpu"));
  });

  it("keeps Other processes in the flow when the right-aligned slot has no room", () => {
    // Eight PM2 apps reach past where the box would start (744): it follows them instead.
    const g = reference();
    for (const k of ["d", "e", "f", "g", "h"]) g.nodes.push(node(`pm2:/h/.pm2:${k}`, "pm2", "pm2"));
    expect(defaultLayout(g, 1072)["proc:900"]).toEqual({ x: 846 + PAD_X, y: 386 });
    // A GPU box wider than the canvas: right-aligned, the box would end past the canvas edge.
    const narrow: G = {
      groups: [
        { id: "hw", kind: "lane", label: "hw", row: 0 },
        { id: "gpu", kind: "lane", label: "GPU · a label wider than this narrow canvas", row: 0 },
        { id: "other", kind: "host", label: "o", row: 2, underGpu: true },
      ],
      nodes: [node("hw:cpu", "cpu", "hw"), node("gpu:GPU-0", "gpu", "gpu"), node("proc:1", "proc", "other")],
    };
    const q = defaultLayout(narrow, 240);
    expect(groupRect("gpu", narrow, q)!.w).toBeGreaterThan(240);
    expect(q["proc:1"]).toEqual({ x: PAD_X, y: 386 });
    // With room, the GPU box's right edge counts its tab-wide minimum width (132 + 318 − 116 = 334).
    const r = defaultLayout(narrow, 1072);
    expect(groupRect("gpu", narrow, r)).toMatchObject({ x: 132, w: 318 });
    expect(r["proc:1"]).toEqual({ x: 334 + PAD_X, y: 202 });
  });

  it("is deterministic and reports a group's padded rect", () => {
    const g = reference();
    expect(defaultLayout(g, 1072)).toEqual(defaultLayout(g, 1072));
    expect(groupRect("hw", g, defaultLayout(g, 1072))).toEqual({ x: 0, y: 0, w: 422, h: 138 });
    expect(groupRect("nope", g, defaultLayout(g, 1072))).toBeNull();
  });

  it("wraps groups and members within the width", () => {
    const g = busy();
    const p = defaultLayout(g, 700);
    const boxes = g.nodes.map((n) => ({ ...p[n.key], w: NODE_W, h: NODE_H }));
    for (const b of boxes) {
      expect(b.x).toBeGreaterThanOrEqual(10);
      expect(b.y).toBeGreaterThanOrEqual(18);
      expect(b.x + NODE_W).toBeLessThanOrEqual(690);
    }
    expect(anyOverlap(boxes)).toBe(false);
    expect(anyOverlap(g.groups.map((x) => groupRect(x.id, g, p)!))).toBe(false);
  });

  it("keeps saved positions and places newcomers", () => {
    const p = arrangeNodes(withNewcomers(), { "ct:a": { x: 500, y: 600 }, "proc:1": { x: 9, y: 9 } }, 1072);
    expect(p["ct:a"]).toEqual({ x: 500, y: 600 });
    expect(p["ct:b"]).toEqual({ x: 602, y: 600 }); // same group, right of ct:a
    expect(groupRect("compose:new", withNewcomers(), p)!.y).toBe(600 + NODE_H + PAD_BOTTOM + ROW_GAP);
    expect(p["proc:1"]).not.toEqual({ x: 9, y: 9 }); // procs never take saved positions
    // Never saved, `other` keeps its default slot: no placed group box covers it.
    const g = withNewcomers();
    expect(groupRect("other", g, p)).toEqual(groupRect("other", g, defaultLayout(g, 1072)));
  });

  it("keeps a never-saved group in its default slot unless a placed group covers it", () => {
    const g = reference();
    const dflt = defaultLayout(g, 1072);
    const slot = groupRect("other", g, dflt)!;
    // Every savable icon saved where the default puts it: the slot is free.
    const saved = Object.fromEntries(g.nodes.filter((n) => n.persist).map((n) => [n.key, dflt[n.key]]));
    expect(groupRect("other", g, arrangeNodes(g, saved, 1072))).toEqual(slot);
    // A standalone container saved on that slot: `other` goes below everything placed.
    const p = arrangeNodes(g, { ...saved, "ct:s1": { x: slot.x + 10, y: slot.y + 18 } }, 1072);
    const bottom = Math.max(...g.groups.filter((x) => x.id !== "other").map((x) => {
      const r = groupRect(x.id, g, p)!;
      return r.y + r.h;
    }));
    expect(groupRect("other", g, p)).toEqual({ ...slot, y: bottom + ROW_GAP });
    // A box that only shares an edge with the slot leaves it free.
    const t = arrangeNodes(g, { ...saved, "ct:s1": { x: slot.x + PAD_X, y: slot.y - NODE_H - PAD_BOTTOM } }, 1072);
    const solo = groupRect("solo", g, t)!;
    expect(solo.y + solo.h).toBe(slot.y);
    expect(solo.x).toBeLessThan(slot.x + slot.w);
    expect(groupRect("other", g, t)).toEqual(slot);
  });

  it("keeps never-saved icons where they were moved in this view, with newcomers beside them", () => {
    const g = reference();
    g.nodes.push(node("proc:950", "proc", "other"));
    const dflt = defaultLayout(g, 1072);
    const held = { "proc:900": { x: 600, y: 700 } };
    const beside = { x: 600 + NODE_W + NODE_GAP, y: 700 };
    // Nothing saved yet: every savable icon keeps its default spot.
    const p = arrangeNodes(g, {}, 1072, held);
    expect([p["proc:900"], p["proc:950"]]).toEqual([held["proc:900"], beside]);
    for (const n of g.nodes.filter((x) => x.persist)) expect(p[n.key], n.key).toEqual(dflt[n.key]);
    // With a saved layout too; a stored entry for such an icon still places nothing.
    const saved = Object.fromEntries(g.nodes.filter((n) => n.persist).map((n) => [n.key, dflt[n.key]]));
    const q = arrangeNodes(g, { ...saved, "proc:950": { x: 9, y: 9 } }, 1072, held);
    expect([q["proc:900"], q["proc:950"]]).toEqual([held["proc:900"], beside]);
  });

  it("falls back to the default layout when no saved position is usable", () => {
    const g = withNewcomers();
    const saved = { "proc:1": { x: 9, y: 9 }, "ct:a": { x: NaN, y: 1 } };
    expect(arrangeNodes(g, saved, 1072)).toEqual(defaultLayout(g, 1072));
  });

  it("snaps to the 12px grid", () => {
    expect([17, 18, -5].map(snapToGrid)).toEqual([12, 24, 0]);
  });

  it("reads only valid stored layouts", () => {
    for (const bad of ["{bad", '{"v":2,"hosts":{"dev10":{"ct:a":{"x":1,"y":2}}}}']) {
      localStorage.setItem(LAYOUT_KEY, bad);
      expect(loadSavedPositions("dev10")).toEqual({});
    }
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ v: 1, hosts: { dev10: { "ct:a": { x: "1", y: 2 }, "ct:b": { x: 5, y: 6 } } } }));
    expect(loadSavedPositions("dev10")).toEqual({ "ct:b": { x: 5, y: 6 } });
    localStorage.setItem(LAYOUT_KEY, '{"v":1,"hosts":{"dev10":{"ct:a":{"x":1e999,"y":2}}}}');
    expect(loadSavedPositions("dev10")).toEqual({});
  });

  it("keeps the other hosts when saving one", () => {
    savePositions("a", { "ct:x": { x: 1, y: 2 } });
    savePositions("b", { "ct:y": { x: 3, y: 4 } });
    expect(loadSavedPositions("a")).toEqual({ "ct:x": { x: 1, y: 2 } });
    expect(loadSavedPositions("b")).toEqual({ "ct:y": { x: 3, y: 4 } });
  });

  it("treats an agent id such as __proto__ as a plain key", () => {
    const leak = () => [({} as Record<string, unknown>)["ct:api"], (Object as unknown as Record<string, unknown>)["ct:api"]];
    try {
      savePositions("dev10", { "ct:web": { x: 7, y: 8 } });
      savePositions("__proto__", { "ct:api": { x: 1, y: 2 } });
      savePositions("constructor", { "ct:api": { x: 3, y: 4 } });
      expect(leak()).toEqual([undefined, undefined]);
      expect(loadSavedPositions("__proto__")).toEqual({ "ct:api": { x: 1, y: 2 } });
      expect(loadSavedPositions("constructor")).toEqual({ "ct:api": { x: 3, y: 4 } });
      expect(loadSavedPositions("dev10")).toEqual({ "ct:web": { x: 7, y: 8 } });
      expect(loadSavedPositions("toString")).toEqual({});
    } finally {
      // A regression would leave the key on the shared prototypes; keep it out of the other tests.
      delete (Object.prototype as Record<string, unknown>)["ct:api"];
      delete (Object as unknown as Record<string, unknown>)["ct:api"];
    }
  });

  it("keeps the 500 most recently written entries per host", () => {
    for (let i = 0; i < 510; i++) savePositions("dev10", { [`ct:${i}`]: { x: i, y: 0 } });
    savePositions("dev10", { "ct:20": { x: 1, y: 1 } }); // rewriting moves it to the newest end
    const host = JSON.parse(localStorage.getItem(LAYOUT_KEY)!).hosts.dev10;
    expect(Object.keys(host)).toHaveLength(500);
    expect(host["ct:9"]).toBeUndefined(); // 0..9 were the 10 oldest of 510
    expect(host["ct:10"]).toEqual({ x: 10, y: 0 });
    expect(host["ct:20"]).toEqual({ x: 1, y: 1 });
    expect(Object.keys(host).at(-1)).toBe("ct:20");
  });

  it("round-trips line toggles and ignores bad values", () => {
    saveLineToggles({ gpu: false, cpu: true, mem: false });
    expect(loadLineToggles()).toEqual({ gpu: false, cpu: true, mem: false });
    localStorage.setItem("glassops_topology_lines", '{"gpu":"no","cpu":false}');
    expect(loadLineToggles()).toEqual({ gpu: true, cpu: false, mem: true });
  });

  it("survives storage failures", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    expect(() => savePositions("dev10", { "ct:a": { x: 1, y: 1 } })).not.toThrow();
    expect(() => saveLineToggles({ gpu: true, cpu: true, mem: true })).not.toThrow();
    expect(loadSavedPositions("dev10")).toEqual({});
    expect(loadLineToggles()).toEqual({ gpu: true, cpu: true, mem: true });
  });
});
