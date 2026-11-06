import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMetricsStore, type MetricSnapshot } from "../../../stores/metricsStore";
// @ts-expect-error -- the app tsconfig has no Node types; Vitest runs in Node and stubs CSS imports as "".
import { readFileSync } from "node:fs";
import { _resetServerClockForTest } from "../../../utils/serverClock";
import Topology from "../Topology";
import { full } from "./topologyFixtures";

/** The menu bar selects `id`; `snapshot` becomes its newest sample (`null`: none yet). */
function setHost(id: string, snapshot: MetricSnapshot | null) {
  const s = useMetricsStore.getState();
  const history = snapshot ? [...(s.agentId === id ? s.history : []), snapshot] : [];
  useMetricsStore.setState({ agentId: id, selectedAgentId: id, current: snapshot, history });
}

function node(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`.topology-node[data-node-key="${key}"]`);
  if (!el) throw new Error(`no icon ${key}`);
  return el;
}
const labels = () => Array.from(document.querySelectorAll("g.topology-edge-label text"), (t) => t.textContent);
const edges = (kind: string) =>
  Array.from(document.querySelectorAll(`svg.topology-edges path[data-edge-kind="${kind}"]`));
const tip = (key: string) => node(key).querySelector(".topology-tip")!;

describe("Topology", () => {
  beforeEach(() => {
    _resetServerClockForTest();
    // /api/time never answers here: ages use the local clock.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
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

  it("renders every kind of icon and the status bar", () => {
    setHost("dev10", full()); render(<Topology />);
    for (const k of ["hw:cpu", "gpu:GPU-0", "ct:api", "pm2:/home/hspark/.pm2:admin-api", "proc:900"])
      expect(document.querySelector(`[data-node-key="${k}"]`)).not.toBeNull();
    expect(screen.getByText("dev10")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "api, RAM 1.8/4.0 GB" })).toBe(node("ct:api"));
    expect(node("ct:api")).toHaveAttribute("tabindex", "0");
    expect(node("ct:batch")).toHaveClass("is-stopped");
    expect(node("ct:api")).not.toHaveClass("is-stopped");
    expect(screen.queryByText(/predates Topology/)).toBeNull();
  });

  it("waits for data", () => {
    setHost("dev10", null); render(<Topology />);
    expect(screen.getByText("Waiting for data…")).toBeInTheDocument();
    expect(document.querySelector(".topology-canvas")).not.toBeNull();
    expect(document.querySelector(".topology-node")).toBeNull();
  });

  it("shows numbers only on the selected workload's lines", () => {
    setHost("dev10", full()); render(<Topology />);
    fireEvent.click(node("ct:vllm"));
    expect(labels().sort()).toEqual(["max 64.0 GB · reserved 32.0 GB", "max 8 cores", "uses 38.2 GB"]);
    fireEvent.click(node("hw:cpu"));
    expect(labels()).toEqual([]);
    expect(edges("cpu").every((p) => p.classList.contains("is-hl"))).toBe(true);
    fireEvent.keyDown(node("ct:api"), { key: "Enter" });
    expect(node("ct:api")).toHaveClass("is-selected");
    fireEvent.keyDown(node("ct:redis"), { key: " " });
    expect(node("ct:redis")).toHaveClass("is-selected");
    expect(node("ct:api")).not.toHaveClass("is-selected");
  });

  it("focuses the hovered icon over the selected one; an empty-canvas click clears", () => {
    setHost("dev10", full()); render(<Topology />);
    fireEvent.click(node("ct:vllm"));
    expect(node("gpu:GPU-0")).toHaveClass("is-related");
    expect(node("hw:mem")).toHaveClass("is-related");
    expect(node("hw:disk")).not.toHaveClass("is-related");
    const others = edges("mem").filter((p) => p.getAttribute("data-from") !== "ct:vllm");
    expect(others.length).toBeGreaterThan(0);
    expect(others.every((p) => p.classList.contains("is-dim"))).toBe(true);

    fireEvent.mouseEnter(node("ct:api"));
    expect(labels().sort()).toEqual(["max 2 cores", "max 4.0 GB · reserved 2.0 GB"]);
    fireEvent.mouseLeave(node("ct:api"));
    expect(labels()).toContain("uses 38.2 GB");

    fireEvent.click(document.querySelector(".topology-canvas")!);
    expect(document.querySelector(".topology-node.is-selected")).toBeNull();
    expect(labels()).toEqual([]);
    expect(document.querySelector("svg.topology-edges path.is-hl, svg.topology-edges path.is-dim")).toBeNull();
  });

  it("hides a line kind from its chip and remembers it", () => {
    setHost("dev10", full()); const { unmount } = render(<Topology />);
    fireEvent.click(screen.getByRole("button", { name: "CPU limits" }));
    expect(edges("cpu")).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem("glassops_topology_lines")!).cpu).toBe(false);
    expect(screen.getByRole("button", { name: "CPU limits" })).toHaveAttribute("aria-pressed", "false");

    unmount(); render(<Topology />);
    expect(screen.getByRole("button", { name: "CPU limits" })).toHaveAttribute("aria-pressed", "false");
    expect(edges("cpu")).toHaveLength(0);
    expect(edges("mem").length).toBeGreaterThan(0);
  });

  it("warns when the agent is older", () => {
    setHost("dev10", full({ stripNewFields: true })); render(<Topology />);
    expect(screen.getByText(/This host's agent predates Topology/)).toBeInTheDocument();
  });

  it("reports Stale only after three missed intervals", () => {
    vi.useFakeTimers(); setHost("dev10", full({ timestamp: 1000 })); render(<Topology />);
    act(() => setHost("dev10", full({ timestamp: 1030 })));            // 30 s agent interval
    act(() => vi.advanceTimersByTime(20_000)); expect(screen.getByText("Live")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(75_000)); expect(screen.getByText("Stale")).toBeInTheDocument();
    // Back on its 30 s pace: every arrival restarts the count.
    act(() => setHost("dev10", full({ timestamp: 1125 }))); expect(screen.getByText("Live")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(30_000)); act(() => setHost("dev10", full({ timestamp: 1155 })));
    act(() => vi.advanceTimersByTime(80_000)); expect(screen.getByText("Live")).toBeInTheDocument();
    act(() => useMetricsStore.setState({ connected: false })); expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });

  it("clears selection on host switch", () => {
    setHost("dev10", full()); render(<Topology />); fireEvent.click(node("ct:api"));
    fireEvent.mouseEnter(node("ct:vllm"));
    act(() => setHost("dev11", full()));
    expect(document.querySelector(".topology-node.is-selected")).toBeNull();
    expect(labels()).toEqual([]);
    expect(screen.getByText("dev11")).toBeInTheDocument();
  });

  it("keeps tooltips inside the canvas", () => {
    setHost("dev10", full()); render(<Topology />);
    expect(tip("hw:cpu")).toHaveClass("is-below", "is-left");        // top row, left edge
    expect(tip("gpu:GPU-3")).toHaveClass("is-below");
    expect(tip("gpu:GPU-3").className).not.toMatch(/is-left|is-right/);
    expect(tip("ct:train")).toHaveClass("is-right");                  // x 886: near the right edge
    expect(tip("ct:train")).not.toHaveClass("is-below");
    expect(tip("ct:api").className).toBe("topology-tip");
    expect(node("ct:api")).toHaveAccessibleDescription(/img\/api:latest/);
  });

  it("places a showing tooltip inside the visible canvas, also after scrolling", () => {
    setHost("dev10", full()); render(<Topology />);
    const canvas = document.querySelector<HTMLElement>(".topology-canvas")!;
    const gpu = node("gpu:GPU-0");
    const gpuTip = tip("gpu:GPU-0");
    // jsdom has no layout: the canvas shows 640 × 300 px at y 100, and the GPU row has been
    // scrolled up to just under that visible top.
    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 640 });
    Object.defineProperty(canvas, "clientHeight", { configurable: true, value: 300 });
    const box = (left: number, top: number, width: number, height: number) =>
      ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
    let iconTop = 110;
    let tipSize = [0, 0];                                      // hidden first (as while dragged): no size
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      if (this === canvas) return box(0, 100, 640, 300);
      if (this === gpu) return box(300, iconTop, 96, 112);
      if (this === gpuTip) return box(0, 0, tipSize[0], tipSize[1]);
      return box(0, 0, 0, 0);
    });
    fireEvent.mouseEnter(gpu);
    expect(gpuTip).not.toHaveClass("is-placed");               // nothing measured: the class rule stays
    tipSize = [300, 140];
    fireEvent.mouseLeave(gpu);
    fireEvent.mouseEnter(gpu);
    expect(gpuTip).toHaveClass("is-placed");                   // no room above in view: below, centred
    expect(gpuTip).toHaveStyle({ top: "116px", left: "-102px" });
    iconTop = 270;
    fireEvent.scroll(canvas);                                  // scrolled back: room above again
    expect(gpuTip).toHaveStyle({ top: "-144px", left: "-102px" });
  });

  it("keeps the zoom control at its own width when the toolbar is narrow", () => {
    // An `overflow: hidden` flex item may shrink to 0 px: on a 390 px phone − % + vanished.
    // It must keep its width so the toolbar scrolls sideways instead.
    const css: string = readFileSync("src/styles/global.css", "utf8");   // Vitest runs from frontend/
    const rule = /\.topology-zoom \{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/flex:\s*none|flex-shrink:\s*0/);
  });

  it("lays out again for the canvas width on window resize", () => {
    setHost("dev10", full()); render(<Topology />);
    expect(node("gpu:GPU-0").style.top).toBe("18px");                 // 1072 px: Hardware and GPU share a row
    const canvas = document.querySelector<HTMLElement>(".topology-canvas")!;
    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 600 });
    act(() => { window.dispatchEvent(new Event("resize")); });
    expect(node("gpu:GPU-0").style.top).toBe("202px");                // 552 px: GPU wraps below Hardware
  });
});
