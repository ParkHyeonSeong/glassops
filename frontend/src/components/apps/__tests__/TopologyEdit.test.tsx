import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMetricsStore, type MetricSnapshot } from "../../../stores/metricsStore";
import { _resetServerClockForTest } from "../../../utils/serverClock";
import Topology from "../Topology";
import { NODE_GAP, NODE_W, PAD_TOP, PAD_X, snapToGrid, type Point } from "../topologyLayout";
import { full } from "./topologyFixtures";

/** The menu bar selects `id`; `snapshot` becomes its newest sample. */
function setHost(id: string, snapshot: MetricSnapshot | null) {
  const s = useMetricsStore.getState();
  const history = snapshot ? [...(s.agentId === id ? s.history : []), snapshot] : [];
  useMetricsStore.setState({ agentId: id, selectedAgentId: id, current: snapshot, history });
}

function mount() {
  setHost("dev10", full());
  return render(<Topology />);
}

function node(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.topology-node[data-node-key="${key}"]`);
  if (!el) throw new Error(`no icon ${key}`);
  return el;
}
function group(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.topology-group[data-group-id="${id}"]`);
  if (!el) throw new Error(`no group ${id}`);
  return el;
}
const tab = (id: string) => group(id).querySelector<HTMLElement>(".topology-group-tab")!;
const at = (key: string): Point => ({ x: parseFloat(node(key).style.left), y: parseFloat(node(key).style.top) });
/** Every icon's position by key. */
const layout = (): Record<string, Point> =>
  Object.fromEntries(Array.from(document.querySelectorAll<HTMLElement>(".topology-node"), (n) => [n.dataset.nodeKey!, at(n.dataset.nodeKey!)]));
/** Only the icons that are remembered: outside-container processes (`proc:`) never are. */
const remembered = (l: Record<string, Point>) => Object.fromEntries(Object.entries(l).filter(([k]) => !k.startsWith("proc:")));
/** The saved layout of `host`. */
function stored(host: string): Record<string, Point> {
  const raw = localStorage.getItem("glassops_topology_layout");
  return raw ? (JSON.parse(raw).hosts[host] ?? {}) : {};
}
const unlock = () => fireEvent.click(screen.getByRole("button", { name: "Layout locked" }));
const zoomIn = () => fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));

const POINTER = { pointerId: 1 };
const pointerDown = (el: Element, x: number, y: number) =>
  fireEvent.pointerDown(el, { ...POINTER, button: 0, clientX: x, clientY: y });
const pointerMove = (x: number, y: number) => fireEvent.pointerMove(window, { ...POINTER, clientX: x, clientY: y });
const pointerUp = () => fireEvent.pointerUp(window, POINTER);
function drag(el: Element, dx: number, dy: number) {
  pointerDown(el, 100, 100);
  pointerMove(100 + dx, 100 + dy);
  pointerUp();
}
function canvasSize(width: number, height: number) {
  const canvas = document.querySelector(".topology-canvas")!;
  Object.defineProperty(canvas, "clientWidth", { configurable: true, value: width });
  Object.defineProperty(canvas, "clientHeight", { configurable: true, value: height });
}

/** compose:shop in graph order (by name): ct:api is the anchor that snaps when its tab is dragged. */
const SHOP = ["ct:api", "ct:batch", "ct:rabbitmq", "ct:redis"];

/** Animation frames wait here, by id, until `runFrames`; cancelling one removes it. */
let frames = new Map<number, FrameRequestCallback>();
let lastFrame = 0;
/** Runs the waiting frames at time `now` (default: far in the future, so Auto-arrange finishes). */
function runFrames(now = performance.now() + 60_000) {
  act(() => {
    const due = [...frames.values()];
    frames.clear();
    for (const cb of due) cb(now);
  });
}

describe("Topology arranging", () => {
  beforeEach(() => {
    _resetServerClockForTest();
    // /api/time never answers here: ages use the local clock.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    frames = new Map();
    lastFrame = 0;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.set(++lastFrame, cb);
      return lastFrame;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
    localStorage.clear();
    useMetricsStore.setState({
      agents: {},
      agentIds: [],
      selectedAgentId: null,
      connected: true,
      current: null,
      history: [],
      agentId: null,
    });
  });

  afterEach(() => {
    cleanup();
    _resetServerClockForTest();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not move icons while locked", () => {
    mount();
    const start = at("ct:api");
    drag(node("ct:api"), 50, 50);
    expect(at("ct:api")).toEqual(start);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
    expect(screen.getByRole("button", { name: "Layout locked" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("button", { name: "Auto-arrange" })).toBeNull();
    expect(tab("compose:shop").querySelector(".topology-grip")).toBeNull();
    // Locked again from the toolbar: still nothing moves.
    unlock(); fireEvent.click(screen.getByRole("button", { name: "Editing" }));
    drag(node("ct:api"), 50, 50);
    expect(at("ct:api")).toEqual(start);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
  });

  it("drags an icon in edit mode and saves it", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    mount(); unlock();
    expect(screen.getByRole("button", { name: "Editing" })).toHaveAttribute("aria-pressed", "true");
    expect(document.querySelector(".topology-editbar")).toHaveTextContent("Editing — drag icons or group tabs to move them");
    const before = layout();
    const { x: x0, y: y0 } = before["ct:api"];
    const capture = vi.fn();
    node("ct:api").setPointerCapture = capture;

    drag(node("ct:api"), 30, 50);
    expect(capture).toHaveBeenCalledWith(POINTER.pointerId);
    const dropped = { x: snapToGrid(x0 + 30), y: snapToGrid(y0 + 50) };
    expect(at("ct:api")).toEqual(dropped);
    expect(stored("dev10")["ct:api"]).toEqual(dropped);
    expect(screen.getByText("Saved ✓")).toBeInTheDocument();
    // It moves alone: the other remembered icons stay where they were and are saved there too.
    const others = remembered(before);
    delete others["ct:api"];
    for (const [key, p] of Object.entries(others)) expect(at(key), key).toEqual(p);
    expect(stored("dev10")).toEqual({ ...others, "ct:api": dropped });

    act(() => { vi.advanceTimersByTime(1399); });
    expect(screen.getByText("Saved ✓")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.queryByText("Saved ✓")).toBeNull();

    // An outside-container process can be dragged, but it is never saved (so nothing is).
    drag(node("proc:900"), 30, 0);
    expect(Object.keys(stored("dev10")).filter((k) => k.startsWith("proc:"))).toEqual([]);
    expect(screen.queryByText("Saved ✓")).toBeNull();
  });

  it("drags a whole group by its tab and keeps its spacing", () => {
    mount(); unlock();
    expect(tab("compose:shop").querySelector(".topology-grip")).not.toBeNull();
    const before = SHOP.map(at);
    const boxLeft = parseFloat(group("compose:shop").style.left);
    const llmLeft = group("compose:llm").style.left;

    drag(tab("compose:shop"), 24, 0);
    // The anchor snaps to the grid and every member moves by the same amount.
    const dx = snapToGrid(before[0].x + 24) - before[0].x;
    const dy = snapToGrid(before[0].y) - before[0].y;
    expect(SHOP.map(at)).toEqual(before.map((p) => ({ x: p.x + dx, y: p.y + dy })));
    expect(parseFloat(group("compose:shop").style.left)).toBe(boxLeft + dx);
    expect(group("compose:llm").style.left).toBe(llmLeft);
    for (const key of SHOP) expect(stored("dev10")[key]).toEqual(at(key));

    // Past the top-left edge the group stops with its left-most and top-most members at 0 and
    // keeps its spacing, even with members left of and above the anchor.
    drag(node("ct:batch"), -300, 0);
    drag(node("ct:rabbitmq"), 0, -100);
    const spread = SHOP.map(at);
    drag(tab("compose:shop"), -2000, -2000);
    const left = Math.min(...spread.map((p) => p.x));
    const top = Math.min(...spread.map((p) => p.y));
    expect([left, top]).toEqual([spread[1].x, spread[2].y]); // ct:batch is left of the anchor, ct:rabbitmq above it
    expect([left < spread[0].x, top < spread[0].y]).toEqual([true, true]);
    expect(SHOP.map(at)).toEqual(spread.map((p) => ({ x: p.x - left, y: p.y - top })));
  });

  it("follows the pointer at 150% zoom", () => {
    mount(); unlock();
    zoomIn();
    expect(screen.getByText("110%")).toBeInTheDocument();
    for (let i = 0; i < 4; i++) zoomIn();
    expect(screen.getByText("150%")).toBeInTheDocument();
    const content = document.querySelector<HTMLElement>(".topology-content")!;
    expect(content.style.transform).toBe("scale(1.5)");
    // The margins scale too, so a box dragged to the top edge keeps its tab in view; the sizer
    // gives the canvas the zoomed extent, (content + margins) × 1.5, to scroll.
    expect([content.style.left, content.style.top]).toEqual(["36px", "45px"]);
    const sizer = document.querySelector<HTMLElement>(".topology-sizer")!;
    expect([sizer.style.left, sizer.style.top]).toEqual([content.style.left, content.style.top]);
    expect(parseFloat(sizer.style.left) + parseFloat(sizer.style.width)).toBe((parseFloat(content.style.width) + 24) * 1.5);
    expect(parseFloat(sizer.style.top) + parseFloat(sizer.style.height)).toBe((parseFloat(content.style.height) + 30) * 1.5);

    const { x: x0, y: y0 } = at("ct:api");
    drag(node("ct:api"), 30, 30);
    expect(at("ct:api")).toEqual({ x: snapToGrid(x0 + 20), y: snapToGrid(y0 + 20) });
  });

  it("auto-arranges back to the default layout and saves it", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    mount(); unlock();
    const initial = layout();
    drag(node("ct:api"), 300, 300);
    const dropped = at("ct:api");
    const proc = at("proc:900");
    expect(dropped).not.toEqual(initial["ct:api"]);
    act(() => { vi.advanceTimersByTime(1400); }); // the drop's `Saved ✓` is gone

    vi.spyOn(performance, "now").mockReturnValue(1000);
    fireEvent.click(screen.getByRole("button", { name: "Auto-arrange" }));
    // Nothing moves before the first frame, and a press meanwhile drags nothing.
    expect(at("ct:api")).toEqual(dropped);
    drag(node("hw:cpu"), 60, 60);
    expect(at("hw:cpu")).toEqual(initial["hw:cpu"]);
    // Halfway through the 380 ms, ease-out cubic has covered 7/8 of the way.
    runFrames(1000 + 190);
    const part = (a: Point, b: Point) => ({ x: a.x + (b.x - a.x) * 0.875, y: a.y + (b.y - a.y) * 0.875 });
    expect(at("ct:api")).toEqual(part(dropped, initial["ct:api"]));
    const procHalfway = at("proc:900");
    expect(screen.queryByText("Saved ✓")).toBeNull();

    runFrames();
    // Every icon is back, outside-container processes included (they glided to their default
    // slot and stay there), and every remembered icon is saved.
    for (const [key, p] of Object.entries(initial)) expect(at(key), key).toEqual(p);
    expect(procHalfway).toEqual(part(proc, initial["proc:900"]));
    expect(stored("dev10")).toEqual(remembered(initial));
    expect(screen.getByText("Saved ✓")).toBeInTheDocument();
  });

  it("keeps Other processes in its default slot after the first save", () => {
    mount(); unlock();
    const proc = at("proc:900");
    const otherLeft = group("other").style.left;
    const right = (id: string) => parseFloat(group(id).style.left) + parseFloat(group(id).style.width);
    expect(right("other")).toBe(right("gpu")); // the default: right-aligned under the GPU row
    drag(node("ct:api"), 30, 0);
    expect(stored("dev10")["ct:api"]).toEqual(at("ct:api"));
    expect(at("proc:900")).toEqual(proc);
    expect(group("other").style.left).toBe(otherLeft);
    expect(Object.keys(stored("dev10")).filter((k) => k.startsWith("proc:"))).toEqual([]);
  });

  it("keeps a dragged outside-container process where it was dropped, in this view only", () => {
    const { unmount } = mount(); unlock();
    const start = at("proc:900");
    drag(node("proc:900"), 30, 50);
    const dropped = { x: snapToGrid(start.x + 30), y: snapToGrid(start.y + 50) };
    expect(at("proc:900")).toEqual(dropped);
    act(() => setHost("dev10", full({ cpuPercent: 50 }))); // a new snapshot keeps it there
    expect(at("proc:900")).toEqual(dropped);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull(); // nothing is stored
    expect(screen.queryByText("Saved ✓")).toBeNull();

    unmount(); mount(); // a reload forgets it
    expect(at("proc:900")).toEqual(start);
    unlock(); drag(tab("other"), 24, 0); // dragged by its group's tab
    expect(at("proc:900")).not.toEqual(start);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
    act(() => setHost("dev11", full())); act(() => setHost("dev10", full())); // so does a host switch
    expect(at("proc:900")).toEqual(start);

    drag(node("proc:900"), 30, 50);
    fireEvent.click(screen.getByRole("button", { name: "Auto-arrange" })); runFrames(); // and Auto-arrange
    expect(at("proc:900")).toEqual(start);
  });

  it("places a new outside-container process beside its moved group", () => {
    mount(); unlock();
    const cpu = at("hw:cpu");
    drag(tab("other"), 120, 240);
    const held = at("proc:900");
    act(() => setHost("dev10", full({ extraProcess: 950 })));
    expect(at("proc:900")).toEqual(held);
    expect(at("proc:950")).toEqual({ x: held.x + NODE_W + NODE_GAP, y: held.y }); // right of the right-most member
    // The box wraps the two icons there: it does not reach back to the default slot.
    expect([parseFloat(group("other").style.left), parseFloat(group("other").style.top)])
      .toEqual([held.x - PAD_X, held.y - PAD_TOP]);
    expect(at("hw:cpu")).toEqual(cpu); // and nothing else moved
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
  });

  it("stops Auto-arrange unsaved when the host changes", () => {
    mount(); unlock();
    const initial = layout();
    drag(node("ct:api"), 300, 300);
    const dev10 = stored("dev10");
    fireEvent.click(screen.getByRole("button", { name: "Auto-arrange" }));
    act(() => setHost("dev11", full()));
    runFrames();
    expect(stored("dev10")).toEqual(dev10); // nothing more saved for the old host
    expect(stored("dev11")).toEqual({});
    expect(layout()).toEqual(initial); // the new host shows its own (default) layout
  });

  it("stops Auto-arrange unsaved when the app closes", () => {
    const errors = vi.spyOn(console, "error");
    const { unmount } = mount(); unlock();
    drag(node("ct:api"), 300, 300);
    const dev10 = stored("dev10");
    fireEvent.click(screen.getByRole("button", { name: "Auto-arrange" }));
    unmount();
    runFrames();
    expect(stored("dev10")).toEqual(dev10);
    expect(errors).not.toHaveBeenCalled();
  });

  it("keeps the drag when a snapshot arrives mid-drag", () => {
    mount(); unlock();
    const { x: x0, y: y0 } = at("ct:api");
    pointerDown(node("ct:api"), 100, 100); pointerMove(130, 150);
    act(() => setHost("dev10", full({ extraContainer: "new-svc", cpuPercent: 77 })));
    expect(node("ct:new-svc")).toBeInTheDocument();
    expect(node("hw:cpu").querySelector(".topology-badge")).toHaveTextContent("77%");
    expect(at("ct:api")).toEqual({ x: snapToGrid(x0 + 30), y: snapToGrid(y0 + 50) });

    pointerMove(160, 150); pointerUp();
    const dropped = { x: snapToGrid(x0 + 60), y: snapToGrid(y0 + 50) }; // relative to the original start
    expect(at("ct:api")).toEqual(dropped);
    expect(stored("dev10")["ct:api"]).toEqual(dropped);
  });

  it("cancels the drag on host switch without saving", () => {
    mount(); unlock();
    const start = at("ct:api");
    pointerDown(node("ct:api"), 100, 100); pointerMove(150, 150);
    act(() => setHost("dev11", full())); pointerUp();
    expect(stored("dev10")).toEqual({}); expect(stored("dev11")).toEqual({});
    expect(at("ct:api")).toEqual(start);
  });

  it("a click that ends a drag keeps the selection", () => {
    mount(); fireEvent.click(node("hw:cpu")); unlock(); drag(node("ct:api"), 40, 0); fireEvent.click(node("ct:api"));
    expect(node("hw:cpu")).toHaveClass("is-selected");
    expect(node("ct:api")).not.toHaveClass("is-selected");
    // Only that one click is swallowed …
    fireEvent.click(node("ct:api"));
    expect(node("ct:api")).toHaveClass("is-selected");
    // … and after a drop that no click follows (a touch drag), the next press and click select.
    fireEvent.click(node("hw:cpu"));
    drag(node("ct:api"), 40, 0);
    pointerDown(node("ct:api"), 100, 100); pointerUp(); fireEvent.click(node("ct:api"));
    expect(node("ct:api")).toHaveClass("is-selected");
  });

  it("treats a press that moves under 4 px as a click, and 4 px as a drag", () => {
    mount(); unlock();
    const start = at("ct:api");
    pointerDown(node("ct:api"), 100, 100); pointerMove(102, 101); pointerUp(); fireEvent.click(node("ct:api"));
    expect(at("ct:api")).toEqual(start);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
    expect(node("ct:api")).toHaveClass("is-selected");
    // 4 px starts a drag; the drop is saved even where the icon snaps back to its spot.
    pointerDown(node("ct:api"), 100, 100); pointerMove(103, 101); pointerUp();
    expect(stored("dev10")["ct:api"]).toEqual(at("ct:api"));
    expect(screen.getByText("Saved ✓")).toBeInTheDocument();
  });

  it("drags only with the primary button", () => {
    mount(); unlock();
    const start = at("ct:api");
    fireEvent.pointerDown(node("ct:api"), { ...POINTER, button: 2, clientX: 100, clientY: 100 });
    pointerMove(160, 160); pointerUp();
    expect(at("ct:api")).toEqual(start);
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
  });

  it("follows only the pointer that grabbed the icon", () => {
    mount(); unlock();
    drag(node("hw:cpu"), 0, 300); // so Auto-arrange would change something
    const cpu = at("hw:cpu");
    const { x: x0, y: y0 } = at("ct:api");
    pointerDown(node("ct:api"), 100, 100);
    // A second finger: its press, moves and release leave the drag alone, and so does a tap on
    // Auto-arrange.
    const other = { pointerId: 2 };
    fireEvent.pointerDown(node("hw:cpu"), { ...other, button: 0, clientX: 400, clientY: 100 });
    fireEvent.pointerMove(window, { ...other, clientX: 500, clientY: 300 });
    fireEvent.pointerUp(window, other);
    fireEvent.click(screen.getByRole("button", { name: "Auto-arrange" }));
    runFrames();
    expect(at("ct:api")).toEqual({ x: x0, y: y0 });
    expect(at("hw:cpu")).toEqual(cpu);

    pointerMove(130, 150); pointerUp();
    expect(at("ct:api")).toEqual({ x: snapToGrid(x0 + 30), y: snapToGrid(y0 + 50) });
    expect(at("hw:cpu")).toEqual(cpu);
  });

  it("ends a drag unsaved when it is cancelled, its icon disappears or editing stops", () => {
    mount(); unlock();
    const start = at("ct:api");

    pointerDown(node("ct:api"), 100, 100); pointerMove(160, 160);
    fireEvent.pointerCancel(window, POINTER);
    expect(at("ct:api")).toEqual(start);
    pointerMove(200, 200); pointerUp();
    expect(at("ct:api")).toEqual(start);
    pointerDown(node("hw:cpu"), 100, 100); // the next press shows nothing of the cancelled drag
    expect(at("ct:api")).toEqual(start);
    pointerUp();

    pointerDown(node("ct:api"), 100, 100); pointerMove(160, 160);
    const withoutApi = full();
    withoutApi.containers = withoutApi.containers!.filter((c) => c.name !== "api");
    act(() => setHost("dev10", withoutApi));
    act(() => setHost("dev10", full())); // back again: the drag does not resume
    pointerMove(200, 200); pointerUp();
    expect(at("ct:api")).toEqual(start);

    pointerDown(node("ct:api"), 100, 100); pointerMove(160, 160);
    fireEvent.click(screen.getByRole("button", { name: "Lock" }));
    pointerUp();
    expect(at("ct:api")).toEqual(start);
    expect(screen.getByRole("button", { name: "Layout locked" })).toHaveAttribute("aria-pressed", "false");
    expect(localStorage.getItem("glassops_topology_layout")).toBeNull();
  });

  it("clamps zoom and fits content", () => {
    mount();
    for (let i = 0; i < 10; i++) fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(screen.getByText("50%")).toBeInTheDocument();
    canvasSize(2400, 1600); fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(screen.getByText("150%")).toBeInTheDocument();

    // In between, Fit takes the largest 10 % step that shows every group box with the canvas
    // margins, whichever side is tighter.
    const boxes = Array.from(document.querySelectorAll<HTMLElement>(".topology-group"), (g) => ({
      right: parseFloat(g.style.left) + parseFloat(g.style.width),
      bottom: parseFloat(g.style.top) + parseFloat(g.style.height),
    }));
    const right = Math.max(...boxes.map((b) => b.right));
    const bottom = Math.max(...boxes.map((b) => b.bottom));
    for (const [width, height] of [[800, 2400], [2400, 450]]) {
      const fit = Math.floor(Math.min(width / (right + 48), height / (bottom + 60)) * 10) * 10;
      expect(fit).toBeGreaterThan(50); expect(fit).toBeLessThan(150);
      canvasSize(width, height); fireEvent.click(screen.getByRole("button", { name: "Fit" }));
      expect(screen.getByText(`${fit}%`)).toBeInTheDocument();
    }
  });

  it("relocks on reopen", () => {
    const { unmount } = mount(); unlock(); zoomIn(); unmount(); mount();
    expect(screen.getByRole("button", { name: "Layout locked" })).toHaveAttribute("aria-pressed", "false");
    expect(document.querySelector(".topology-editbar")).toBeNull();
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("keeps the arranged layout after a reload", () => {
    const { unmount } = mount(); unlock();
    const start = at("ct:api");
    drag(node("ct:api"), 30, 50);
    const dropped = at("ct:api");
    expect(dropped).not.toEqual(start);
    unmount(); mount();
    expect(at("ct:api")).toEqual(dropped);
    // Each host has its own layout.
    act(() => setHost("dev11", full()));
    expect(at("ct:api")).toEqual(start);
    act(() => setHost("dev10", full()));
    expect(at("ct:api")).toEqual(dropped);
  });

  it("lays out again when the canvas itself is resized", () => {
    class FakeResizeObserver {
      static made: FakeResizeObserver[] = [];
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
      callback: ResizeObserverCallback;
      constructor(callback: ResizeObserverCallback) {
        this.callback = callback;
        FakeResizeObserver.made.push(this);
      }
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
    const { unmount } = mount();
    const canvas = document.querySelector<HTMLElement>(".topology-canvas")!;
    const [observer] = FakeResizeObserver.made;
    // The border box: a scroll bar that comes or goes with the new layout is no resize.
    expect(observer.observe).toHaveBeenCalledWith(canvas, { box: "border-box" });
    expect(node("gpu:GPU-0").style.top).toBe("18px");              // 1072 px: Hardware and GPU share a row

    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 600 });
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    expect(node("gpu:GPU-0").style.top).toBe("202px");             // 552 px: GPU wraps below Hardware
    unmount();
    expect(observer.disconnect).toHaveBeenCalled();
  });
});
