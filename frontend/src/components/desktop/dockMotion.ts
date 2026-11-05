import type { DockPosition, DockSettings } from "../../stores/settingsStore";

// Pure maths behind the dock: item sizes, the spring that moves them, the click hop, the
// room the dock takes from windows and where its context menu goes. No DOM and no React;
// lengths are px and times are seconds.

// Distance between the bar and the screen edge it sits on.
export const DOCK_EDGE = 12;

// Min wins when the range is inverted: a menu wider than the viewport still starts at the
// near margin and overflows on the far side instead of starting off-screen.
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

// ===== Sizes =====

// The settings as lengths on screen, each already multiplied by the shrink-to-fit scale.
export interface DockMetrics {
  base: number; // item size at rest
  peak: number; // item size under the pointer
  thickness: number; // bar thickness
  pad: number; // bar padding between its edge and the items, on every side
  gap: number;
  distance: number; // how far the magnification reaches along the dock
}

// peak and thickness never drop below base, so magnification <= base turns magnifying off
// and a bar thinner than the items still holds them.
export function dockMetrics(settings: DockSettings, scale: number): DockMetrics {
  const base = settings.baseItemSize * scale;
  const thickness = Math.max(settings.baseItemSize, settings.panelHeight) * scale;
  return {
    base,
    peak: Math.max(settings.baseItemSize, settings.magnification) * scale,
    thickness,
    pad: (thickness - base) / 2,
    gap: settings.gap * scale,
    distance: settings.distance * scale,
  };
}

// Target size of an item whose rest centre is `offset` px from the pointer: the peak right
// under it, a raised-cosine falloff over `range`, the base beyond.
export function magnifiedSize(offset: number, base: number, peak: number, range: number): number {
  const r = Math.abs(offset) / range;
  return r < 1 ? base + ((peak - base) * (1 + Math.cos(Math.PI * r))) / 2 : base;
}

// ===== Spring =====

export interface SpringState {
  size: number; // px
  velocity: number; // px/s
}

// Longest slice the spring is integrated in. The stiffest preset swings at about 65 rad/s,
// so slices this short stay accurate and stable however long the frame was. The cost grows
// with the frame, one slice per 1/240 s, so callers clamp dt (the dock to 0.1 s).
const SPRING_SLICE = 1 / 240;

// One frame of a damped spring pulling `size` towards `target`: semi-implicit Euler over
// equal slices of at most SPRING_SLICE, with the damping term solved implicitly so a heavy
// damper cannot overshoot zero velocity within a slice. Pure; the caller keeps the state.
// A dt of zero or less runs no slice and moves nothing.
export function stepSpring(
  state: SpringState,
  target: number,
  dt: number,
  spring: { mass: number; stiffness: number; damping: number },
): SpringState {
  const slices = Math.ceil(dt / SPRING_SLICE);
  const h = dt / slices;
  const pull = spring.stiffness / spring.mass; // acceleration per px of error
  const drag = spring.damping / spring.mass; // deceleration per px/s
  let { size, velocity } = state;
  for (let i = 0; i < slices; i += 1) {
    velocity = (velocity + pull * (target - size) * h) / (1 + drag * h);
    size += velocity * h;
  }
  return { size, velocity };
}

const SETTLE_EPSILON = 0.01;

// Settled once neither the error nor the speed is visible any more.
export function isSettled(state: SpringState, target: number): boolean {
  return Math.abs(state.velocity) < SETTLE_EPSILON && Math.abs(target - state.size) < SETTLE_EPSILON;
}

// ===== Fit =====

const FIT_MIN = 0.4;

// Shrink-to-fit factor: `needed` is the dock's longest length, its rest length plus the most
// its items grow together (peakSwell), `room` what the viewport offers along that side.
export function fitScale(needed: number, room: number): number {
  return needed > 0 ? clamp(room / needed, FIT_MIN, 1) : 1;
}

// The most the items grow together, which is how much longer than at rest the bar gets: their
// summed swell (magnified size − base) for the pointer where that sum is largest. Items whose
// rest centres are `centres` all swell at once, the nearest ones most. Beyond the first or last
// centre every item only gets further away, so the pointer is tried at every px between them,
// which comes within a small fraction of a px of the largest sum.
export function peakSwell(centres: number[], base: number, peak: number, range: number): number {
  if (centres.length === 0) return 0;
  const first = Math.min(...centres);
  const last = Math.max(...centres);
  let most = 0;
  for (let offset = 0; offset < last - first + 1; offset += 1) {
    const at = Math.min(first + offset, last);
    let sum = 0;
    for (const centre of centres) sum += magnifiedSize(at - centre, base, peak, range) - base;
    most = Math.max(most, sum);
  }
  return most;
}

// ===== Hop =====

const HOP_HEIGHT = 0.4; // peak lift, as a share of the item size
const HOP_AIRTIME = 0.38; // s, up and back down for the first arc
const HOP_RESTITUTION = 0.36; // share of the impact speed a rebound keeps
const HOP_MIN_REBOUND = 0.2; // the hop ends when a rebound is slower than this share of launch

// A tile jumping away from the screen edge. Pure; the caller keeps the state.
export interface HopState {
  lift: number; // px away from the edge, never below 0
  velocity: number; // px/s, positive = away from the edge
  launch: number; // px/s, speed of the first take-off
  gravity: number; // px/s², constant for the whole hop
  active: boolean;
}

// Gravity is chosen so that an arc lasting HOP_AIRTIME peaks at HOP_HEIGHT × size.
export function startHop(size: number): HopState {
  const gravity = (8 * HOP_HEIGHT * size) / HOP_AIRTIME ** 2;
  const launch = (gravity * HOP_AIRTIME) / 2;
  return { lift: 0, velocity: launch, launch, gravity, active: true };
}

// Constant gravity makes each arc a parabola, so the step is exact for any dt: it walks from
// landing to landing instead of sampling, and one long frame can cover several rebounds.
export function stepHop(hop: HopState, dt: number): HopState {
  if (!hop.active) return hop;
  const { gravity } = hop;
  let { lift, velocity } = hop;
  let left = dt;
  while (left > 0) {
    // Time until lift + velocity·t − gravity·t²/2 reaches 0.
    const landing = (velocity + Math.sqrt(velocity * velocity + 2 * gravity * lift)) / gravity;
    if (landing > left) {
      lift += velocity * left - (gravity * left * left) / 2;
      velocity -= gravity * left;
      break;
    }
    const rebound = HOP_RESTITUTION * (gravity * landing - velocity);
    if (rebound < HOP_MIN_REBOUND * hop.launch) return { ...hop, lift: 0, velocity: 0, active: false };
    lift = 0;
    velocity = rebound;
    left -= landing;
  }
  return { ...hop, lift: Math.max(0, lift), velocity };
}

// ===== Window reserve =====

export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

// Room the dock keeps free for windows: its edge gap plus the bar on the dock's side, nothing
// with auto-hide. For "top" the inset is measured below the menubar. `scale` defaults to 1
// because window placement does not follow shrink-to-fit.
export function dockReserve(settings: DockSettings, scale = 1): Insets {
  const reserve: Insets = { top: 0, right: 0, bottom: 0, left: 0 };
  if (!settings.autoHide) reserve[settings.position] = DOCK_EDGE + dockMetrics(settings, scale).thickness;
  return reserve;
}

// ===== Menu placement =====

const MENU_GAP = 8; // tail tip to the tile's outer edge
const MENU_TAIL = 9; // tail length
const MENU_OFFSET = MENU_GAP + MENU_TAIL; // menu body to the tile's outer edge
const MENU_MARGIN = 8; // closest the menu gets to the viewport edge
const MENU_TAIL_INSET = 19; // corner radius (10) + half the tail's base (9)

export interface MenuPlacement {
  x: number;
  y: number;
  tail: number; // tail centre, from the menu's left edge (top/bottom dock) or top edge (left/right)
  maxHeight: number; // the menu scrolls inside this
}

// Places a menu of `size` beside the anchor item (the side the dock is on decides which),
// centred on it and kept MENU_MARGIN inside the viewport. When clamped, the tail slides along
// the menu's edge so it still points at the item.
export function placeMenu(
  anchor: { left: number; top: number; right: number; bottom: number },
  size: { width: number; height: number },
  side: DockPosition,
  viewport: { width: number; height: number },
): MenuPlacement {
  if (side === "bottom" || side === "top") {
    const cx = (anchor.left + anchor.right) / 2;
    const x = clamp(cx - size.width / 2, MENU_MARGIN, viewport.width - MENU_MARGIN - size.width);
    const tail = clamp(cx - x, MENU_TAIL_INSET, size.width - MENU_TAIL_INSET);
    if (side === "bottom") {
      // Above the dock the menu grows upwards, so its top depends on its (capped) height.
      const maxHeight = anchor.top - MENU_OFFSET - MENU_MARGIN;
      return { x, y: anchor.top - MENU_OFFSET - Math.min(size.height, maxHeight), tail, maxHeight };
    }
    const y = anchor.bottom + MENU_OFFSET;
    return { x, y, tail, maxHeight: viewport.height - y - MENU_MARGIN };
  }
  const cy = (anchor.top + anchor.bottom) / 2;
  const maxHeight = viewport.height - 2 * MENU_MARGIN;
  const height = Math.min(size.height, maxHeight);
  const y = clamp(cy - height / 2, MENU_MARGIN, viewport.height - MENU_MARGIN - height);
  return {
    x: side === "left" ? anchor.right + MENU_OFFSET : anchor.left - MENU_OFFSET - size.width,
    y,
    tail: clamp(cy - y, MENU_TAIL_INSET, height - MENU_TAIL_INSET),
    maxHeight,
  };
}
