// Topology: the selected host's hardware and workloads as desktop icons, with lines for
// GPU use/reservation and container CPU/memory limits. The pure modules do the work:
// topologyModel (snapshot → icons, groups, lines), topologyLayout (positions, storage),
// topologyGeometry (curves, label boxes). This file renders and handles focus, arranging
// (lock, drag, auto-arrange) and zoom.
import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  Box,
  Cpu,
  EthernetPort,
  GripVertical,
  HardDrive,
  Hexagon,
  LayoutGrid,
  Lock,
  LockOpen,
  MemoryStick,
  Server,
  SquareTerminal,
} from "lucide-react";
import { useServerNow } from "../../hooks/useServerNow";
import { useMetricsStore, type MetricSnapshot } from "../../stores/metricsStore";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { PORT, anchorsFor, placeLabels, placeTip, routeEdge, type Route } from "./topologyGeometry";
import { GpuGlyph, WhaleGlyph } from "./topologyGlyphs";
import {
  NODE_W,
  arrangeNodes,
  defaultLayout,
  groupRect,
  loadLineToggles,
  loadSavedPositions,
  saveLineToggles,
  savePositions,
  snapToGrid,
  type LineToggles,
  type Point,
  type Positions,
  type Rect,
} from "./topologyLayout";
import { buildTopology, type EdgeKind, type NodeKind, type TopoEdge, type TopoGroup, type TopoNode } from "./topologyModel";

/** The content layer's offset inside the canvas; the space above row 1 holds the folder tabs. */
const CONTENT_X = 24;
const CONTENT_Y = 30;
/** Canvas width assumed while it cannot be measured. */
const FALLBACK_CANVAS_WIDTH = 1120;
/** Tooltips open below icons above this y and hang inward this close to the canvas sides. */
const TIP_BELOW_Y = 150;
const TIP_EDGE_X = 120;
const STALE_MIN_MS = 5000;
const STATUS_TICK_MS = 1000;
/** A press becomes a drag once the pointer has moved this far (|dx| + |dy|, screen px). */
const DRAG_THRESHOLD = 4;
const ARRANGE_MS = 380;
const SAVED_FLASH_MS = 1400;
const ZOOM_MIN = 50;
const ZOOM_MAX = 150;
const ZOOM_STEP = 10;

const clampZoom = (zoom: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));

const OUTDATED_NOTICE =
  "This host's agent predates Topology: CPU/memory limits, GPU reservations and PM2 apps are not shown.";

const CHIPS: { line: keyof LineToggles; label: string }[] = [
  { line: "gpu", label: "GPU" },
  { line: "cpu", label: "CPU limits" },
  { line: "mem", label: "Memory limits" },
];
const LINE_OF: Record<EdgeKind, keyof LineToggles> = { gpu: "gpu", gpuReserved: "gpu", cpu: "cpu", mem: "mem" };
const EDGE_CLASS: Record<EdgeKind, string> = {
  gpu: "is-gpu",
  gpuReserved: "is-gpu-reserved",
  cpu: "is-cpu",
  mem: "is-mem",
};
const WORKLOADS: ReadonlySet<NodeKind> = new Set<NodeKind>(["container", "pm2", "proc"]);

/** Estimated from the text: measuring the DOM would force a layout on every focus change. */
const labelWidth = (text: string) => Math.round(text.length * 6.4 + 16);

interface DrawnEdge {
  id: string;
  edge: TopoEdge;
  route: Route;
}

/** One drag of an icon, or of a group by its tab; fixed from the press to the drop. */
interface DragSession {
  host: string;
  pointerId: number;
  /** Pointer position at the press (screen px) and the zoom factor then. */
  origin: Point;
  scale: number;
  /** What moves; the first is the anchor that snaps to the grid (a tab's first member). */
  nodes: TopoNode[];
  start: Positions;
  /** Remembered icons without a saved spot yet: saved with the drop, so they stay put. */
  pin: Positions;
  grabbedNode: string | null;
  grabbedGroup: string | null;
}

/** Auto-arrange: icons glide from `from` to `to`, then `keep` is saved. */
interface Arrangement {
  host: string;
  from: Positions;
  to: Positions;
  keep: Positions;
}

/** The positions of the icons that are remembered (outside-container processes never are). */
function persistable(nodes: TopoNode[], positions: Positions): Positions {
  const out: Positions = {};
  for (const n of nodes) if (n.persist && positions[n.key]) out[n.key] = positions[n.key];
  return out;
}

/**
 * Where a drag puts its icons once the pointer has moved (dx, dy) screen px: the anchor snaps
 * to the grid and the others move by the same amount, so a group keeps its spacing. The move
 * stops where the left-most or top-most icon reaches 0.
 */
function dragTo(s: DragSession, dx: number, dy: number): Positions {
  const anchor = s.start[s.nodes[0].key];
  const starts = s.nodes.map((n) => s.start[n.key]);
  const sx = Math.max(snapToGrid(anchor.x + dx / s.scale) - anchor.x, -Math.min(...starts.map((p) => p.x)));
  const sy = Math.max(snapToGrid(anchor.y + dy / s.scale) - anchor.y, -Math.min(...starts.map((p) => p.y)));
  const out: Positions = {};
  for (const { key } of s.nodes) out[key] = { x: s.start[key].x + sx, y: s.start[key].y + sy };
  return out;
}

/** Positions part-way (t = 0…1) from `from` to `to`. */
function between(from: Positions, to: Positions, t: number): Positions {
  const out: Positions = {};
  for (const [key, b] of Object.entries(to)) {
    const a = from[key];
    out[key] = { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
  }
  return out;
}

const LUCIDE = { size: 26, strokeWidth: 1.6, "aria-hidden": true } as const;

function NodeGlyph({ kind }: { kind: NodeKind }) {
  switch (kind) {
    case "cpu": return <Cpu {...LUCIDE} />;
    case "mem": return <MemoryStick {...LUCIDE} />;
    case "disk": return <HardDrive {...LUCIDE} />;
    case "net": return <EthernetPort {...LUCIDE} />;
    case "gpu": return <GpuGlyph />;
    case "container": return <WhaleGlyph />;
    case "pm2": return <Hexagon {...LUCIDE} />;
    case "proc": return <SquareTerminal {...LUCIDE} />;
  }
}

interface NodeViewProps {
  node: TopoNode;
  x: number;
  y: number;
  selected: boolean;
  related: boolean;
  dragging: boolean;
  tipBelow: boolean;
  tipAlign: "left" | "right" | null;
  /** Measured on hover/focus (placeTip); wins over tipBelow/tipAlign, which are the fallback. */
  tipAt?: Point;
  onSelect: (key: string) => void;
  onHover: (key: string | null) => void;
  onFocusKey: (key: string | null) => void;
}

const NodeView = memo(function NodeView({
  node, x, y, selected, related, dragging, tipBelow, tipAlign, tipAt, onSelect, onHover, onFocusKey,
}: NodeViewProps) {
  const tipId = useId();
  const className = ["topology-node", `is-${node.kind}`, selected && "is-selected", related && "is-related",
    node.state === "stopped" && "is-stopped", dragging && "is-dragging"].filter(Boolean).join(" ");
  const tipClassName = tipAt
    ? "topology-tip is-placed"
    : ["topology-tip", tipBelow && "is-below", tipAlign && `is-${tipAlign}`].filter(Boolean).join(" ");
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    onSelect(node.key);
  };
  const meter = node.meter && Number.isFinite(node.meter.percent) ? Math.min(100, Math.max(0, node.meter.percent)) : 0;

  return (
    <div
      className={className}
      data-node-key={node.key}
      role="button"
      tabIndex={0}
      aria-label={`${node.name}, ${node.sub}`}
      aria-describedby={tipId}
      style={{ left: x, top: y }}
      onClick={() => onSelect(node.key)}
      onKeyDown={onKeyDown}
      onMouseEnter={() => onHover(node.key)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onFocusKey(node.key)}
      onBlur={() => onFocusKey(null)}
    >
      <div className="topology-icon">
        <NodeGlyph kind={node.kind} />
        {node.badge && <span className={`topology-badge is-${node.badge.level}`}>{node.badge.text}</span>}
        {WORKLOADS.has(node.kind) && (
          <span className={node.state === "stopped" ? "topology-dot is-off" : "topology-dot"} />
        )}
      </div>
      <div className="topology-name">{node.name}</div>
      <div className="topology-sub">{node.sub}</div>
      {node.meter && (
        <div className="topology-meter">
          <i className={`is-${node.meter.level}`} style={{ width: `${meter}%` }} />
        </div>
      )}
      <div className={tipClassName} id={tipId} role="tooltip" style={tipAt ? { left: tipAt.x, top: tipAt.y } : undefined}>
        <div className="topology-tip-title">{node.tip.title}</div>
        {node.tip.rows.map((row, i) => (
          <div key={i} className={row.tone ? `topology-tip-row is-${row.tone}` : "topology-tip-row"}>
            {row.label && <span className="topology-tip-k">{row.label}</span>}
            <span className="topology-tip-v">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
});

/** Time between the two newest snapshots in ms (their timestamps are seconds); 1000 when unknown. */
function snapshotIntervalMs(history: MetricSnapshot[]): number {
  const n = history.length;
  if (n < 2) return 1000;
  const ms = (history[n - 1].timestamp - history[n - 2].timestamp) * 1000;
  return Number.isFinite(ms) && ms > 0 ? ms : 1000;
}

/**
 * Live / Stale / Disconnected, re-evaluated every second. A slow agent (say a 30 s interval)
 * only turns Stale after three missed snapshots. Its own component so the tick does not
 * re-render the map.
 */
function LiveIndicator({ waiting }: { waiting: boolean }) {
  const connected = useMetricsStore((s) => s.connected);
  const intervalMs = useMetricsStore((s) => snapshotIntervalMs(s.history));
  const [lastArrival, setLastArrival] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => useMetricsStore.subscribe((s, prev) => {
    if (s.current !== prev.current) setLastArrival(Date.now());
  }), []);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), STATUS_TICK_MS);
    return () => clearInterval(timer);
  }, []);

  if (connected && waiting) return null;
  const state = !connected ? "off" : now - lastArrival > Math.max(STALE_MIN_MS, 3 * intervalMs) ? "stale" : "live";
  return (
    <span className={`topology-live is-${state}`}>
      <i aria-hidden="true" />
      {state === "off" ? "Disconnected" : state === "stale" ? "Stale" : "Live"}
    </span>
  );
}

export default function Topology() {
  const current = useMetricsStore((s) => s.current);
  const agentId = useMetricsStore((s) => s.agentId);
  const thresholds = useThresholdsStore((s) => s.thresholds);
  const nowMs = useServerNow() * 1000;
  const linesLabelId = useId();

  const canvasRef = useRef<HTMLDivElement>(null);
  const [layoutWidth, setLayoutWidth] = useState(FALLBACK_CANVAS_WIDTH - 2 * CONTENT_X);
  const [lines, setLines] = useState<LineToggles>(loadLineToggles);
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  /** Where the last hovered or focused icon's tooltip goes, measured from the visible canvas. */
  const [tipAt, setTipAt] = useState<{ key: string; at: Point } | null>(null);
  /** The icons whose tooltip shows (hover first, then keyboard focus), for re-placing on scroll. */
  const tipFor = useRef<{ hover: string | null; focus: string | null }>({ hover: null, focus: null });
  // Arranging: locked and at 100 % on every open.
  const [editing, setEditing] = useState(false);
  const [zoom, setZoom] = useState(100);
  /** The shown host's saved layout: read when the host changes, updated by every save. */
  const [saved, setSaved] = useState<Positions>(() => loadSavedPositions(agentId ?? ""));
  /** Outside-container processes moved in this view: never stored, so a reload or host switch drops them. */
  const [held, setHeld] = useState<Positions>({});
  const [drag, setDrag] = useState<DragSession | null>(null);
  const [arrangement, setArrangement] = useState<Arrangement | null>(null);
  /** Where the icons of a drag or Auto-arrange are now: shown over the layout while `by` runs. */
  const [moving, setMoving] = useState<{ by: DragSession | Arrangement; at: Positions } | null>(null);
  /** Non-zero while `Saved ✓` shows; every save bumps it, which restarts the timer. */
  const [savedFlash, setSavedFlash] = useState(0);
  /** Store spots for a host, show them at once, and flash `Saved ✓`. */
  const commit = useCallback((host: string, changed: Positions) => {
    savePositions(host, changed);
    setSaved((prev) => ({ ...prev, ...changed }));
    setSavedFlash((n) => n + 1);
  }, []);
  /** Set by a drop: the click that ends a drag must not change the selection. */
  const swallowClick = useRef(false);
  const [shownHost, setShownHost] = useState(agentId);
  if (shownHost !== agentId) {
    // Another host in the menu bar: its own map and layout, nothing focused, any drag or
    // Auto-arrange dropped unsaved.
    setShownHost(agentId);
    setSelected(null);
    setHovered(null);
    setDrag(null);
    setArrangement(null);
    setSaved(loadSavedPositions(agentId ?? ""));
    setHeld({});
  }

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    const measure = () => setLayoutWidth((canvasRef.current?.clientWidth || FALLBACK_CANVAS_WIDTH) - 2 * CONTENT_X);
    measure();
    window.addEventListener("resize", measure);
    // Resizing or maximising the app window fires no window resize. The border box ignores a
    // scroll bar that comes or goes with the new layout, so a re-layout never measures itself.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (canvas) observer?.observe(canvas, { box: "border-box" });
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, []);

  const graph = useMemo(
    () => (current ? buildTopology(current, nowMs, thresholds) : null),
    [current, nowMs, thresholds],
  );
  const positions = useMemo<Positions>(
    () => (graph ? arrangeNodes(graph, saved, layoutWidth, held) : {}),
    [graph, saved, held, layoutWidth],
  );
  const nodeByKey = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.key, n])), [graph]);
  // A new snapshot keeps the drag, unless a dragged icon has gone away: then it ends unsaved.
  if (drag && drag.nodes.some((n) => !nodeByKey.has(n.key))) setDrag(null);
  const shown = useMemo<Positions>(
    () => (moving && (moving.by === drag || moving.by === arrangement) ? { ...positions, ...moving.at } : positions),
    [positions, moving, drag, arrangement],
  );

  // Group boxes and their extent; the canvas scrolls over it plus the same margin on the far sides.
  const content = useMemo(() => {
    const groups: { group: TopoGroup; rect: Rect }[] = [];
    let right = 0;
    let bottom = 0;
    if (graph) {
      for (const group of graph.groups) {
        const rect = groupRect(group.id, graph, shown);
        if (!rect) continue;
        groups.push({ group, rect });
        right = Math.max(right, rect.x + rect.w);
        bottom = Math.max(bottom, rect.y + rect.h);
      }
    }
    return { groups, right, bottom, width: right + CONTENT_X, height: bottom + CONTENT_Y };
  }, [graph, shown]);

  const drawn = useMemo(() => {
    const out: DrawnEdge[] = [];
    for (const edge of graph?.edges ?? []) {
      if (!lines[LINE_OF[edge.kind]]) continue;
      const from = nodeByKey.get(edge.from);
      const to = nodeByKey.get(edge.to);
      const a = shown[edge.from];
      const b = shown[edge.to];
      if (!from || !to || !a || !b) continue;
      out.push({
        id: `${edge.kind}|${edge.from}|${edge.to}`,
        edge,
        route: routeEdge(anchorsFor(a, from.kind), anchorsFor(b, to.kind), PORT[edge.kind]),
      });
    }
    return out;
  }, [graph, nodeByKey, shown, lines]);

  // Focus = the hovered icon, else the selected one (an icon that has gone away focuses nothing).
  const focus = [hovered, selected].find((key): key is string => key !== null && nodeByKey.has(key)) ?? null;

  const focused = useMemo(() => {
    const related = new Set<string>();
    const labelled: DrawnEdge[] = [];
    if (focus !== null) {
      for (const d of drawn) {
        if (d.edge.from === focus) {
          related.add(d.edge.to);
          labelled.push(d); // numbers only on the focused workload's own lines
        } else if (d.edge.to === focus) {
          related.add(d.edge.from);
        }
      }
    }
    const boxes = placeLabels(labelled.map((d) => d.route), labelled.map((d) => labelWidth(d.edge.label)));
    return { related, labelled, boxes };
  }, [drawn, focus]);

  // A tooltip is placed from what is on screen when it shows: the canvas's visible box (so its
  // scroll position), the icon's box (zoom included) and the tooltip's own size. Without a laid-out
  // canvas the class rule (tipBelow/tipAlign) stays.
  const placeTipOf = useCallback((key: string | null) => {
    if (key === null) return;
    const canvas = canvasRef.current;
    const nodeEl = canvas
      ? [...canvas.querySelectorAll<HTMLElement>(".topology-node")].find((el) => el.dataset.nodeKey === key)
      : undefined;
    const tipEl = nodeEl?.querySelector<HTMLElement>(".topology-tip");
    if (!canvas || !nodeEl || !tipEl || canvas.clientWidth === 0 || canvas.clientHeight === 0) {
      setTipAt(null);
      return;
    }
    const c = canvas.getBoundingClientRect();
    const left = c.left + canvas.clientLeft;
    const top = c.top + canvas.clientTop;
    const view = { left, top, right: left + canvas.clientWidth, bottom: top + canvas.clientHeight };
    const n = nodeEl.getBoundingClientRect();
    const t = tipEl.getBoundingClientRect();
    if (t.width === 0 || t.height === 0) {
      setTipAt(null); // hidden (an icon being dragged): nothing to measure, the class rule stays
      return;
    }
    const at = placeTip(view, n, { width: t.width, height: t.height }, n.width / NODE_W || 1);
    // Unchanged on most scroll events: keep the same state so nothing re-renders.
    setTipAt((prev) => (prev?.key === key && prev.at.x === at.x && prev.at.y === at.y ? prev : { key, at }));
  }, []);
  const onNodeHover = useCallback((key: string | null) => {
    setHovered(key);
    tipFor.current.hover = key;
    placeTipOf(key);
  }, [placeTipOf]);
  const onNodeFocus = useCallback((key: string | null) => {
    tipFor.current.focus = key;
    placeTipOf(key);
  }, [placeTipOf]);
  const onCanvasScroll = useCallback(() => placeTipOf(tipFor.current.hover ?? tipFor.current.focus), [placeTipOf]);

  const toggleLine = (line: keyof LineToggles) => {
    const next = { ...lines, [line]: !lines[line] };
    setLines(next);
    saveLineToggles(next);
  };

  const onCanvasClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if ((e.target as Element).closest(".topology-node")) return;
    setSelected(null);
  };

  // The drag in progress follows the pointer anywhere on the page; the drop saves it. Ending the
  // drag any other way (host change, leaving edit mode, a vanished icon) clears `drag`, which
  // removes these listeners before the pointer is released.
  useEffect(() => {
    if (!drag) return;
    let moved = false;
    let at = drag.start;
    const mine = (e: PointerEvent) => e.pointerId === drag.pointerId;
    const onMove = (e: PointerEvent) => {
      if (!mine(e)) return;
      const dx = e.clientX - drag.origin.x;
      const dy = e.clientY - drag.origin.y;
      if (!moved && Math.abs(dx) + Math.abs(dy) < DRAG_THRESHOLD) return;
      moved = true;
      at = dragTo(drag, dx, dy);
      setMoving({ by: drag, at });
    };
    const onUp = (e: PointerEvent) => {
      if (!mine(e)) return;
      if (moved) {
        const keep = persistable(drag.nodes, at);
        if (Object.keys(keep).length > 0) commit(drag.host, { ...drag.pin, ...keep });
        // Outside-container processes are never stored: a moved one keeps its spot in this view.
        const loose: Positions = {};
        for (const n of drag.nodes) if (!n.persist) loose[n.key] = at[n.key];
        setHeld((prev) => ({ ...prev, ...loose }));
        swallowClick.current = true;
      }
      setDrag(null);
    };
    const onCancel = (e: PointerEvent) => {
      if (mine(e)) setDrag(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, [drag, commit]);

  // Auto-arrange glides every icon to the default layout (ease-out cubic), then saves it.
  useEffect(() => {
    if (!arrangement) return;
    const t0 = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / ARRANGE_MS);
      if (t < 1) {
        setMoving({ by: arrangement, at: between(arrangement.from, arrangement.to, 1 - (1 - t) ** 3) });
        frame = requestAnimationFrame(step);
        return;
      }
      commit(arrangement.host, arrangement.keep);
      setHeld({}); // outside-container processes are back in their default slot too
      setArrangement(null);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [arrangement, commit]);

  useEffect(() => {
    if (savedFlash === 0) return;
    const timer = setTimeout(() => setSavedFlash(0), SAVED_FLASH_MS);
    return () => clearTimeout(timer);
  }, [savedFlash]);

  const onCanvasPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    swallowClick.current = false; // a new press: only the click that ends a drag is swallowed
    if (!editing || e.button !== 0 || !graph || drag || arrangement) return;
    const target = e.target as Element;
    const icon = target.closest<HTMLElement>(".topology-node");
    const handle = icon ?? target.closest<HTMLElement>(".topology-group-tab");
    if (!handle) return;
    const grabbedNode = icon?.dataset.nodeKey ?? null;
    const grabbedGroup = icon ? null : (handle.parentElement?.dataset.groupId ?? null);
    const nodes = graph.nodes.filter((n) => (icon ? n.key === grabbedNode : n.groupId === grabbedGroup));
    const start: Positions = {};
    for (const n of nodes) start[n.key] = positions[n.key];
    setDrag({
      host: agentId ?? "",
      pointerId: e.pointerId,
      origin: { x: e.clientX, y: e.clientY },
      scale: zoom / 100,
      nodes,
      start,
      pin: persistable(graph.nodes.filter((n) => !saved[n.key]), positions),
      grabbedNode,
      grabbedGroup,
    });
    if (typeof handle.setPointerCapture === "function") handle.setPointerCapture(e.pointerId);
  };

  const onCanvasClickCapture = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!swallowClick.current) return;
    swallowClick.current = false;
    e.stopPropagation();
  };

  const setEditMode = (on: boolean) => {
    setEditing(on);
    setDrag(null); // leaving edit mode drops a drag unsaved
  };

  const autoArrange = () => {
    if (!graph || drag) return;
    const to = defaultLayout(graph, layoutWidth);
    const next: Arrangement = { host: agentId ?? "", from: shown, to, keep: persistable(graph.nodes, to) };
    setMoving({ by: next, at: next.from });
    setArrangement(next);
  };

  const zoomBy = (step: number) => setZoom((z) => clampZoom(z + step));
  const fit = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const factor = Math.min(
      canvas.clientWidth / (content.right + 2 * CONTENT_X),
      canvas.clientHeight / (content.bottom + 2 * CONTENT_Y),
    );
    setZoom(clampZoom(Math.floor(factor * 10) * 10));
  };
  const scale = zoom / 100;

  return (
    <div className="topology-app">
      <div className="topology-toolbar">
        <button type="button" className="topology-lock" aria-pressed={editing} onClick={() => setEditMode(!editing)}>
          {editing ? <LockOpen size={14} aria-hidden="true" /> : <Lock size={14} aria-hidden="true" />}
          {editing ? "Editing" : "Layout locked"}
        </button>
        <span className="topology-sep" aria-hidden="true" />
        <div className="topology-lines" role="group" aria-labelledby={linesLabelId}>
          <span id={linesLabelId}>Lines</span>
          {CHIPS.map(({ line, label }) => (
            <button
              key={line}
              type="button"
              className={`topology-chip is-${line}`}
              aria-pressed={lines[line]}
              onClick={() => toggleLine(line)}
            >
              <i aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
        <div className="topology-zoom">
          <button type="button" aria-label="Zoom out" onClick={() => zoomBy(-ZOOM_STEP)}>−</button>
          <span className="topology-zoom-value">{`${zoom}%`}</span>
          <button type="button" aria-label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)}>+</button>
        </div>
        <button type="button" className="topology-fit" onClick={fit}>Fit</button>
      </div>

      <div className="topology-stage">
        <div
          className={editing ? "topology-canvas is-editing" : "topology-canvas"}
          ref={canvasRef}
          onClick={onCanvasClick}
          onClickCapture={onCanvasClickCapture}
          onPointerDown={onCanvasPointerDown}
          onScroll={onCanvasScroll}
        >
          {graph ? (
            <>
              {/* Zoom scales the content and its margins. Scaling leaves the layout size alone, so
                  this box gives the canvas the zoomed extent to scroll. */}
              <div
                className="topology-sizer"
                style={{
                  left: CONTENT_X * scale,
                  top: CONTENT_Y * scale,
                  width: content.width * scale,
                  height: content.height * scale,
                }}
              />
              <div
                className="topology-content"
                style={{
                  left: CONTENT_X * scale,
                  top: CONTENT_Y * scale,
                  width: content.width,
                  height: content.height,
                  transform: `scale(${scale})`,
                }}
              >
                {content.groups.map(({ group, rect }) => (
                  <div
                    key={group.id}
                    className={`topology-group is-${group.kind}${drag?.grabbedGroup === group.id ? " is-dragging" : ""}`}
                    data-group-id={group.id}
                    style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
                  >
                    <div className="topology-group-tab">
                      {editing && <GripVertical className="topology-grip" size={12} aria-hidden="true" />}
                      {group.kind === "compose" && <Box size={12} aria-hidden="true" />}
                      {group.label}
                      {group.tag && <span className="topology-group-tag">{group.tag}</span>}
                    </div>
                  </div>
                ))}
                <svg className="topology-edges" width={content.width} height={content.height} aria-hidden="true">
                  {drawn.map(({ id, edge, route }) => {
                    const touches = edge.from === focus || edge.to === focus;
                    const state = focus === null ? "" : touches ? " is-hl" : " is-dim";
                    return (
                      <Fragment key={id}>
                        <path
                          className={`topology-edge ${EDGE_CLASS[edge.kind]}${state}`}
                          d={route.d}
                          data-edge-kind={edge.kind}
                          data-from={edge.from}
                          data-to={edge.to}
                        />
                        <circle
                          className={`topology-edge-dot ${EDGE_CLASS[edge.kind]}${state}`}
                          cx={route.end.x}
                          cy={route.end.y}
                          r={touches ? 3.6 : 2.6}
                        />
                      </Fragment>
                    );
                  })}
                </svg>
                {graph.nodes.map((n) => {
                  const p = shown[n.key];
                  if (!p) return null;
                  return (
                    <NodeView
                      key={n.key}
                      node={n}
                      x={p.x}
                      y={p.y}
                      selected={n.key === selected}
                      related={focused.related.has(n.key)}
                      dragging={drag?.grabbedNode === n.key}
                      tipBelow={p.y < TIP_BELOW_Y}
                      tipAlign={p.x < TIP_EDGE_X ? "left" : p.x + NODE_W > layoutWidth - TIP_EDGE_X ? "right" : null}
                      tipAt={tipAt?.key === n.key ? tipAt.at : undefined}
                      onSelect={setSelected}
                      onHover={onNodeHover}
                      onFocusKey={onNodeFocus}
                    />
                  );
                })}
                {/* Above icons and tabs, so a nudged label stays readable; below a hovered icon's tooltip. */}
                <svg className="topology-edge-labels" width={content.width} height={content.height} aria-hidden="true">
                  {focused.labelled.map(({ id, edge }, i) => {
                    const box = focused.boxes[i];
                    return (
                      <g key={id} className={`topology-edge-label ${EDGE_CLASS[edge.kind]}`}>
                        <rect x={box.x} y={box.y} width={box.w} height={box.h} rx={9} />
                        <text x={box.x + box.w / 2} y={box.y + 12.6} textAnchor="middle">{edge.label}</text>
                      </g>
                    );
                  })}
                </svg>
              </div>
            </>
          ) : (
            <div className="topology-empty">Waiting for data…</div>
          )}
        </div>
        {editing && (
          <div className="topology-editbar">
            <span className="topology-editbar-hint"><b>Editing</b> — drag icons or group tabs to move them</span>
            <span className="topology-saved" aria-live="polite">{savedFlash > 0 && "Saved ✓"}</span>
            <button type="button" onClick={autoArrange}>
              <LayoutGrid size={13} aria-hidden="true" />
              Auto-arrange
            </button>
            <button type="button" className="is-primary" onClick={() => setEditMode(false)}>
              <Lock size={13} aria-hidden="true" />
              Lock
            </button>
          </div>
        )}
      </div>

      <div className="topology-status">
        <Server size={13} aria-hidden="true" />
        {agentId && <b className="topology-host">{agentId}</b>}
        {graph && <span className="topology-summary">{graph.summary}</span>}
        {graph?.outdatedAgent && (
          <span className="topology-notice" title={OUTDATED_NOTICE}>{OUTDATED_NOTICE}</span>
        )}
        <LiveIndicator waiting={!graph} />
      </div>
    </div>
  );
}
