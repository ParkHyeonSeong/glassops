import type { Point } from "../topologyLayout";
import { describe, expect, it } from "vitest";
import { anchorsFor, placeLabels, placeTip, PORT, routeEdge, type LabelBox, type Route } from "../topologyGeometry";

function overlaps(a: LabelBox, b: LabelBox): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

describe("topologyGeometry", () => {
  it("computes anchors per node kind", () => {
    expect(anchorsFor({ x: 10, y: 18 }, "cpu")).toEqual({ cx: 58, top: 24, bottom: 126, mid: 51, left: 32, right: 84 });
    expect(anchorsFor({ x: 10, y: 18 }, "container").bottom).toBe(116);
    expect(PORT).toEqual({ cpu: -14, mem: 14, gpu: 0, gpuReserved: 0 });
  });

  it("routes up to a resource above", () => {
    const r = routeEdge(anchorsFor({ x: 10, y: 202 }, "container"), anchorsFor({ x: 10, y: 18 }, "cpu"), -14);
    expect(r.d).toBe("M44,208 C44,160 58,160 58,126");
    expect(r.pointAt(0)).toEqual({ x: 44, y: 208 });
    expect(r.end).toEqual({ x: 58, y: 126 });
  });

  it("routes down and sideways", () => {
    expect(routeEdge(anchorsFor({ x: 10, y: 18 }, "cpu"), anchorsFor({ x: 10, y: 202 }, "container"), 0).d)
      .toBe("M58,126 C58,174 58,174 58,208");
    expect(routeEdge(anchorsFor({ x: 10, y: 18 }, "container"), anchorsFor({ x: 300, y: 18 }, "container"), 0).d)
      .toBe("M84,51 C203,51 203,51 322,51");
    expect(routeEdge(anchorsFor({ x: 300, y: 18 }, "container"), anchorsFor({ x: 10, y: 18 }, "container"), 0).d)
      .toBe("M322,51 C203,51 203,51 84,51");
  });

  it("clamps the bend to half the vertical gap", () => {
    const near = routeEdge(anchorsFor({ x: 10, y: 120 }, "container"), anchorsFor({ x: 10, y: 0 }, "container"), 0);
    // y1=126, y2=98, k=min(34,14)=14
    expect(near.d).toBe("M58,126 C58,112 58,112 58,98");
    const nearDown = routeEdge(anchorsFor({ x: 10, y: 0 }, "container"), anchorsFor({ x: 10, y: 120 }, "container"), 14);
    // y1=98, y2=126, k=14, port 14 -> x1=72
    expect(nearDown.d).toBe("M72,98 C72,112 58,112 58,126");
  });

  it("keeps labels apart", () => {
    const a = routeEdge(anchorsFor({ x: 10, y: 202 }, "container"), anchorsFor({ x: 10, y: 18 }, "cpu"), 0);
    const b = routeEdge(anchorsFor({ x: 20, y: 202 }, "container"), anchorsFor({ x: 20, y: 18 }, "cpu"), 0);
    const [la, lb] = placeLabels([a, b], [80, 80]);
    const p = a.pointAt(0.8);
    expect(la).toEqual({ x: p.x - 40, y: p.y - 9, w: 80, h: 18 });
    expect(overlaps(la, lb)).toBe(false);
  });

  it("nudges the last candidate down in steps of h + 4 until clear", () => {
    const r = routeEdge(anchorsFor({ x: 10, y: 202 }, "container"), anchorsFor({ x: 10, y: 18 }, "cpu"), 0);
    const boxes = placeLabels([r, r, r, r, r, r, r], Array(7).fill(400));
    const p = r.pointAt(0.94);
    const last = boxes[6];
    expect(last.x).toBe(p.x - 200);
    expect((last.y - (p.y - 9)) % 22).toBeCloseTo(0, 9);
    expect(last.y).toBeGreaterThan(p.y - 9);
    for (let i = 0; i < 6; i++) expect(overlaps(last, boxes[i])).toBe(false);
  });

  it("keeps two labels at the same spot apart", () => {
    const r = routeEdge(anchorsFor({ x: 10, y: 202 }, "container"), anchorsFor({ x: 10, y: 18 }, "cpu"), 0);
    const [a, b] = placeLabels([r, r], [80, 80]);
    expect(overlaps(a, b)).toBe(false);
    expect(b.y).toBeGreaterThan(a.y);
  });

  it("requires 4 px horizontal and 3 px vertical clearance", () => {
    const stub = (at: Point): Route => ({
      d: "", end: at, pointAt: (t) => (t === 0.8 ? at : { x: 1000 + t, y: 0 }),
    });
    const first = stub({ x: 0, y: 0 });
    const clearOf = (second: Point) => placeLabels([first, stub(second)], [80, 80])[1];
    // 3 px gap sideways (< 4): rejected, falls to the next candidate
    expect(clearOf({ x: 83, y: 0 }).x).toBeCloseTo(1000.72 - 40, 9);
    // exactly 4 px sideways: accepted
    expect(clearOf({ x: 84, y: 0 })).toEqual({ x: 44, y: -9, w: 80, h: 18 });
    // 2 px gap vertically (< 3): rejected
    expect(clearOf({ x: 0, y: 20 }).x).toBeCloseTo(1000.72 - 40, 9);
    // exactly 3 px vertically: accepted
    expect(clearOf({ x: 0, y: 21 })).toEqual({ x: -40, y: 12, w: 80, h: 18 });
  });

  // placeTip: screen boxes in, the tooltip's offset from its icon (icon px) out.
  const icon = (left: number, top: number) => ({ left, top, right: left + 96, bottom: top + 112 });
  const tip = { width: 300, height: 140 };

  it("places a tooltip inside the visible part of the canvas", () => {
    const view = { left: 0, top: 100, right: 640, bottom: 400 };
    expect(placeTip(view, icon(300, 110), tip, 1)).toEqual({ x: -102, y: 116 });   // scrolled under the top: below
    expect(placeTip(view, icon(300, 270), tip, 1)).toEqual({ x: -102, y: -144 });  // room above: above
    expect(placeTip({ ...view, bottom: 700 }, icon(300, 300), tip, 1).y).toBe(-144); // room both ways: above
    expect(placeTip(view, icon(560, 270), tip, 1).x).toBe(336 - 560);              // pulled in from the right edge
    expect(placeTip(view, icon(-40, 270), tip, 1).x).toBe(4 + 40);                 // and from the left edge
  });

  it("uses the roomier side and keeps a tooltip that fits nowhere readable from its top", () => {
    const view = { left: 0, top: 100, right: 640, bottom: 300 };
    // 50 px above and 38 px below the icon: neither fits, so above, pulled down to the visible top.
    expect(placeTip(view, icon(300, 150), tip, 1).y).toBe(104 - 150);
    expect(placeTip(view, icon(300, 150), { width: 300, height: 250 }, 1).y).toBe(104 - 150); // taller than the view
  });

  it("measures the offset in the icon's own px at any zoom", () => {
    const view = { left: 0, top: 100, right: 640, bottom: 600 };
    // 150 %: icon, tooltip and gap are 1.5× on screen; the offset is the 100 % one.
    expect(placeTip(view, { left: 300, top: 110, right: 444, bottom: 278 }, { width: 450, height: 210 }, 1.5))
      .toEqual({ x: -102, y: 116 });
  });
});
