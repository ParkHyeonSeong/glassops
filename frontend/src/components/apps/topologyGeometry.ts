import type { EdgeKind, NodeKind } from "./topologyModel";
import type { Point } from "./topologyLayout";

export interface Anchors { cx: number; top: number; bottom: number; mid: number; left: number; right: number }

const METERED: ReadonlySet<NodeKind> = new Set<NodeKind>(["cpu", "mem", "disk", "gpu"]);

export function anchorsFor(pos: Point, kind: NodeKind): Anchors {
  return {
    cx: pos.x + 48,
    top: pos.y + 6,
    bottom: pos.y + (METERED.has(kind) ? 108 : 98),
    mid: pos.y + 33,
    left: pos.x + 22,
    right: pos.x + 74,
  };
}

export const PORT: Record<EdgeKind, number> = { cpu: -14, mem: 14, gpu: 0, gpuReserved: 0 };

export interface Route { d: string; end: Point; pointAt(t: number): Point }

export function routeEdge(from: Anchors, to: Anchors, port: number): Route {
  let p0: Point, p1: Point, p2: Point, p3: Point;
  if (to.bottom + 8 < from.top) {
    const x1 = from.cx + port, y1 = from.top, x2 = to.cx, y2 = to.bottom;
    const k = Math.min(34, (y1 - y2) / 2);
    p0 = { x: x1, y: y1 }; p1 = { x: x1, y: y2 + k }; p2 = { x: x2, y: y2 + k }; p3 = { x: x2, y: y2 };
  } else if (to.top - 8 > from.bottom) {
    const x1 = from.cx + port, y1 = from.bottom, x2 = to.cx, y2 = to.top;
    const k = Math.min(34, (y2 - y1) / 2);
    p0 = { x: x1, y: y1 }; p1 = { x: x1, y: y2 - k }; p2 = { x: x2, y: y2 - k }; p3 = { x: x2, y: y2 };
  } else {
    const toRight = to.cx >= from.cx;
    const x1 = toRight ? from.right : from.left, y1 = from.mid;
    const x2 = toRight ? to.left : to.right, y2 = to.mid;
    const dx = x2 - x1;
    p0 = { x: x1, y: y1 }; p1 = { x: x1 + dx / 2, y: y1 }; p2 = { x: x2 - dx / 2, y: y2 }; p3 = { x: x2, y: y2 };
  }
  const d = `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`;
  const pointAt = (t: number): Point => {
    const u = 1 - t;
    const a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, e = t * t * t;
    return { x: a * p0.x + b * p1.x + c * p2.x + e * p3.x, y: a * p0.y + b * p1.y + c * p2.y + e * p3.y };
  };
  return { d, end: p3, pointAt };
}

export interface LabelBox { x: number; y: number; w: number; h: number }

const LABEL_H = 18;
const CANDIDATES = [0.8, 0.72, 0.88, 0.64, 0.56, 0.94];
const CLEAR_X = 4;
const CLEAR_Y = 3;

function collides(a: LabelBox, b: LabelBox): boolean {
  return a.x < b.x + b.w + CLEAR_X && b.x < a.x + a.w + CLEAR_X
    && a.y < b.y + b.h + CLEAR_Y && b.y < a.y + a.h + CLEAR_Y;
}

export function placeLabels(routes: Route[], widths: number[]): LabelBox[] {
  const placed: LabelBox[] = [];
  routes.forEach((route, i) => {
    const w = widths[i];
    const boxAt = (t: number): LabelBox => {
      const p = route.pointAt(t);
      return { x: p.x - w / 2, y: p.y - LABEL_H / 2, w, h: LABEL_H };
    };
    let chosen: LabelBox | undefined;
    for (const t of CANDIDATES) {
      const box = boxAt(t);
      if (!placed.some((o) => collides(box, o))) { chosen = box; break; }
    }
    if (!chosen) {
      const last = boxAt(CANDIDATES[CANDIDATES.length - 1]);
      // Nudge down by h + 4 until clear; terminates because the boxes are finite.
      let box: LabelBox = last;
      while (placed.some((o) => collides(box, o))) box = { ...box, y: box.y + LABEL_H + 4 };
      chosen = box;
    }
    placed.push(chosen);
  });
  return placed;
}

/** A box in viewport pixels, as getBoundingClientRect reports it. */
export interface ScreenBox { left: number; top: number; right: number; bottom: number }

const TIP_GAP = 4;    // between an icon and its tooltip, in the content's own px (it scales with zoom)
const TIP_MARGIN = 4; // kept between a tooltip and the edge of the visible canvas, in screen px

/** Clamp into [lo, hi]; when the range is empty (the box is bigger than the view) keep lo. */
const clampLow = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, hi));

/**
 * Where an icon's tooltip goes so that it can be read in the part of the canvas visible now:
 * above the icon when it fits there, else below, else on the roomier side, then pulled inside the
 * visible area (left/right too). All boxes are screen boxes, so scrolling and zoom are already in
 * them; the result is relative to the icon's top-left in the icon's own (unzoomed) px.
 */
export function placeTip(view: ScreenBox, node: ScreenBox, tip: { width: number; height: number }, scale: number): Point {
  const gap = TIP_GAP * scale;
  const above = node.top - gap - tip.height;
  const below = node.bottom + gap;
  const top = above >= view.top + TIP_MARGIN ? above
    : below + tip.height <= view.bottom - TIP_MARGIN ? below
    : node.top - view.top >= view.bottom - node.bottom ? above : below;
  const centred = (node.left + node.right) / 2 - tip.width / 2;
  return {
    x: (clampLow(centred, view.left + TIP_MARGIN, view.right - TIP_MARGIN - tip.width) - node.left) / scale,
    y: (clampLow(top, view.top + TIP_MARGIN, view.bottom - TIP_MARGIN - tip.height) - node.top) / scale,
  };
}
