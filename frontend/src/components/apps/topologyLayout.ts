// Pure layout + browser persistence for the Topology canvas. No React, no store.
import type { TopoGroup, TopoNode, TopologyGraph } from "./topologyModel";

export const NODE_W = 96, NODE_H = 112, NODE_GAP = 6, PAD_X = 10, PAD_TOP = 18, PAD_BOTTOM = 8,
  GROUP_GAP = 16, ROW_GAP = 46, GRID = 12;

export type Point = { x: number; y: number };
export type Positions = Record<string, Point>;
export interface Rect { x: number; y: number; w: number; h: number }
export type LineToggles = { gpu: boolean; cpu: boolean; mem: boolean };
type LayoutGraph = Pick<TopologyGraph, "nodes" | "groups">;

export const LAYOUT_KEY = "glassops_topology_layout";
export const LINES_KEY = "glassops_topology_lines";
const MAX_ENTRIES_PER_HOST = 500;

export function snapToGrid(v: number): number {
  return Math.max(0, Math.round(v / GRID) * GRID);
}

const isPoint = (v: unknown): v is Point =>
  !!v && typeof v === "object" &&
  Number.isFinite((v as Point).x) && Number.isFinite((v as Point).y) &&
  typeof (v as Point).x === "number" && typeof (v as Point).y === "number";

/** Estimated width in px of a group's folder tab (label, compose icon, tag, padding), a little generous. */
export function tabWidth(group: Pick<TopoGroup, "kind" | "label" | "tag">): number {
  return Math.ceil(
    group.label.length * 6.4 + (group.kind === "compose" ? 18 : 0) + (group.tag ? group.tag.length * 5.5 + 18 : 0) + 18,
  );
}

/** A group box is never narrower than its tab: 12 px in on the left, 12 px clear on the right. */
function minGroupWidth(group: Pick<TopoGroup, "kind" | "label" | "tag"> | undefined): number {
  return group ? 12 + tabWidth(group) + 12 : 0;
}

export function groupRect(groupId: string, graph: LayoutGraph, positions: Positions): Rect | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of graph.nodes) {
    if (n.groupId !== groupId) continue;
    const p = positions[n.key];
    if (!p) continue;
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + NODE_W);
    maxY = Math.max(maxY, p.y + NODE_H);
  }
  if (minX === Infinity) return null;
  const minW = minGroupWidth(graph.groups.find((g) => g.id === groupId));
  const w = Math.max(maxX - minX + 2 * PAD_X, minW);
  return { x: minX - PAD_X, y: minY - PAD_TOP, w, h: maxY - minY + PAD_TOP + PAD_BOTTOM };
}

export function defaultLayout(graph: LayoutGraph, maxWidth: number): Positions {
  const perLine = Math.max(1, Math.floor((maxWidth - 2 * PAD_X + NODE_GAP) / (NODE_W + NODE_GAP)));
  const members = new Map<string, string[]>();
  for (const n of graph.nodes) {
    const list = members.get(n.groupId);
    if (list) list.push(n.key); else members.set(n.groupId, [n.key]);
  }
  const gpuGroupId = graph.nodes.find((n) => n.kind === "gpu")?.groupId;
  const out: Positions = {};
  let lineTop = 0, lineH = 0, cursor = 0, lineRow: number | null = null;
  let gpuSlot: { x: number; w: number } | null = null;

  for (const g of graph.groups) {
    const keys = members.get(g.id);
    if (!keys || keys.length === 0) continue;
    const cols = Math.min(keys.length, perLine);
    const lines = Math.ceil(keys.length / perLine);
    const w = Math.max(cols * NODE_W + (cols - 1) * NODE_GAP + 2 * PAD_X, minGroupWidth(g));
    const h = PAD_TOP + lines * NODE_H + (lines - 1) * NODE_GAP + PAD_BOTTOM;

    if (lineRow === null) {
      lineRow = g.row;
    } else if (g.row !== lineRow || (cursor > 0 && cursor + w > maxWidth)) {
      lineTop += lineH + ROW_GAP;
      lineH = 0; cursor = 0; lineRow = g.row;
    }
    let x = cursor;
    if (g.underGpu && gpuSlot !== null) {
      // Right-aligned under the GPU group when that keeps the flow and the canvas width (§4.7).
      const right = gpuSlot.x + gpuSlot.w - w;
      if (right >= cursor && right + w <= maxWidth) x = right;
    }

    keys.forEach((key, i) => {
      out[key] = {
        x: x + PAD_X + (i % perLine) * (NODE_W + NODE_GAP),
        y: lineTop + PAD_TOP + Math.floor(i / perLine) * (NODE_H + NODE_GAP),
      };
    });
    if (g.id === gpuGroupId) gpuSlot = { x, w };
    cursor = x + w + GROUP_GAP;
    lineH = Math.max(lineH, h);
  }
  return out;
}

/**
 * Where every icon goes. A saved spot counts only for an icon that is saved; `held` gives the
 * icons that never are (outside-container processes) their spots for this view, which a saved
 * entry cannot.
 */
export function arrangeNodes(graph: LayoutGraph, saved: Positions, maxWidth: number, held: Positions = {}): Positions {
  const spot = (n: TopoNode): Point | null => {
    const p = n.persist ? saved[n.key] : held[n.key];
    return isPoint(p) ? p : null;
  };
  if (!graph.nodes.some((n) => spot(n))) return defaultLayout(graph, maxWidth);
  const anySaved = graph.nodes.some((n) => n.persist && spot(n));

  const out: Positions = {};
  for (const n of graph.nodes) {
    const p = spot(n);
    if (p) out[n.key] = { x: p.x, y: p.y };
  }
  const byGroup = new Map<string, typeof graph.nodes>();
  for (const n of graph.nodes) {
    const list = byGroup.get(n.groupId);
    if (list) list.push(n); else byGroup.set(n.groupId, [n]);
  }

  // Newcomers of groups that already have a placed member: right of its right-most member.
  const unanchored: string[] = [];
  for (const g of graph.groups) {
    const nodes = byGroup.get(g.id) ?? [];
    if (nodes.length === 0) continue;
    if (!nodes.some((n) => out[n.key])) { unanchored.push(g.id); continue; }
    for (const n of nodes) {
      if (out[n.key]) continue;
      let right: Point | null = null;
      for (const m of nodes) {
        const p = out[m.key];
        if (p && (right === null || p.x > right.x)) right = p;
      }
      out[n.key] = { x: right!.x + NODE_W + NODE_GAP, y: right!.y };
    }
  }

  // Groups without any placed member: default shape, moved below everything placed. While nothing
  // is saved (only never-saved icons were moved) they keep their default slot, and so does a group
  // that is never saved while no placed group box covers that slot.
  const bottomOf = (): number => {
    let b = -Infinity;
    for (const g of graph.groups) {
      const r = groupRect(g.id, graph, out);
      if (r) b = Math.max(b, r.y + r.h);
    }
    return b;
  };
  const covered = (slot: Rect): boolean =>
    graph.groups.some((g) => {
      const r = groupRect(g.id, graph, out);
      return r !== null && r.x < slot.x + slot.w && slot.x < r.x + r.w && r.y < slot.y + slot.h && slot.y < r.y + r.h;
    });
  if (unanchored.length > 0) {
    const dflt = defaultLayout(graph, maxWidth);
    for (const id of unanchored) {
      const nodes = byGroup.get(id)!;
      const r = groupRect(id, { nodes, groups: graph.groups }, dflt);
      if (!r) continue;
      const stays = !anySaved || (nodes.every((n) => !n.persist) && !covered(r));
      const dy = stays ? 0 : bottomOf() + ROW_GAP - r.y;
      for (const n of nodes) out[n.key] = { x: dflt[n.key].x, y: dflt[n.key].y + dy };
    }
  }
  return out;
}

// ---- browser persistence -------------------------------------------------

function cleanPositions(raw: unknown): Positions {
  const out: Positions = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (k !== "__proto__" && isPoint(v)) out[k] = { x: v.x, y: v.y };
  }
  return out;
}

/** Saved layouts by agent id, in an object without a prototype: any agent id, `__proto__` too, is a plain key. */
function readHosts(): Record<string, Positions> {
  const out: Record<string, Positions> = Object.create(null);
  try {
    const parsed = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "null");
    const hosts: unknown = parsed?.v === 1 ? parsed.hosts : null;
    if (hosts && typeof hosts === "object") {
      for (const [id, h] of Object.entries(hosts)) out[id] = cleanPositions(h);
    }
  } catch {
    /* unreadable: nothing is saved */
  }
  return out;
}

export function loadSavedPositions(agentId: string): Positions {
  return readHosts()[agentId] ?? {};
}

export function savePositions(agentId: string, changed: Positions): void {
  try {
    const hosts = readHosts();
    const host = hosts[agentId] ?? {};
    for (const [k, p] of Object.entries(changed)) {
      if (k === "__proto__" || !isPoint(p)) continue;
      delete host[k]; // re-insert at the end: insertion order = write order
      host[k] = { x: p.x, y: p.y };
    }
    const keys = Object.keys(host);
    const trimmed: Positions = {};
    for (const k of keys.slice(Math.max(0, keys.length - MAX_ENTRIES_PER_HOST))) trimmed[k] = host[k];
    hosts[agentId] = trimmed;
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ v: 1, hosts }));
  } catch {
    /* storage unavailable or full: the layout simply is not remembered */
  }
}

export function loadLineToggles(): LineToggles {
  const t: LineToggles = { gpu: true, cpu: true, mem: true };
  try {
    const raw = localStorage.getItem(LINES_KEY);
    if (!raw) return t;
    const p = JSON.parse(raw);
    if (!p || typeof p !== "object") return t;
    for (const k of ["gpu", "cpu", "mem"] as const) if (typeof p[k] === "boolean") t[k] = p[k];
  } catch {
    /* defaults */
  }
  return t;
}

export function saveLineToggles(t: LineToggles): void {
  try {
    localStorage.setItem(LINES_KEY, JSON.stringify({ gpu: t.gpu, cpu: t.cpu, mem: t.mem }));
  } catch {
    /* ignore */
  }
}
