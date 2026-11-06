// Pure model for the Topology canvas: turns one live metrics snapshot into
// icons (nodes), boxes (groups), lines (edges) and their texts. No React, no store.
import type { ContainerInfo, GpuMetrics, MetricSnapshot, Pm2App } from "../../stores/metricsStore";
import { severityFor, type MetricKey, type Threshold } from "../../lib/thresholds";
import { formatBytes, formatRate } from "../viz/format";

export type NodeKind = "cpu" | "mem" | "disk" | "net" | "gpu" | "container" | "pm2" | "proc";
export type GroupKind = "lane" | "compose" | "solo" | "host";
export type EdgeKind = "gpu" | "gpuReserved" | "cpu" | "mem";
export type Level = "ok" | "warn" | "crit" | "idle";
export interface TipRow { label?: string; value: string; tone?: "cpu" | "mem" | "gpu" | "warn" | "hint" }
export interface TopoNode {
  key: string; kind: NodeKind; groupId: string; name: string; sub: string;
  badge?: { text: string; level: Level }; meter?: { percent: number; level: Level };
  state?: "running" | "stopped"; persist: boolean; tip: { title: string; rows: TipRow[] };
}
export interface TopoGroup { id: string; kind: GroupKind; label: string; tag?: string; row: 0 | 1 | 2; underGpu?: boolean }
export interface TopoEdge { from: string; to: string; kind: EdgeKind; label: string }
export interface TopologyGraph { nodes: TopoNode[]; groups: TopoGroup[]; edges: TopoEdge[]; summary: string; outdatedAgent: boolean }

const GiB = 1024 ** 3;

/** VRAM as the lines and tooltips print it. */
const gb = (bytes: number) => `${(bytes / GiB).toFixed(1)} GB`;

/** Integers print without decimals, others with up to two. */
function num(n: number): string {
  return String(Number(n.toFixed(2)));
}

export function formatPair(used: number, total: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (i < units.length - 1 && total >= 1024 ** (i + 1)) i++;
  const f = (v: number) => {
    const x = v / 1024 ** i;
    return x < 100 ? x.toFixed(1) : x.toFixed(0);
  };
  return `${f(used)}/${f(total)} ${units[i]}`;
}

export function formatAge(ms: number): string {
  const s = Math.floor(Math.max(0, ms) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

const DOCKER_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/;

export function parseDockerTime(value?: string): number | null {
  if (!value) return null;
  const m = DOCKER_TIME.exec(value);
  if (!m) return null;
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map(Number);
  const ms = Number(((m[7] ?? "") + "000").slice(0, 3));
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 60) return null;
  const date = new Date(0);
  date.setUTCFullYear(y, mo - 1, d); // not Date.UTC: it maps years 0-99 to 19xx
  date.setUTCHours(h, mi, s, ms);
  let t = date.getTime();
  if (m[8] !== "Z") {
    const sign = m[8][0] === "-" ? -1 : 1;
    t -= sign * (Number(m[8].slice(1, 3)) * 60 + Number(m[8].slice(4, 6))) * 60_000;
  }
  return Number.isFinite(t) ? t : null;
}

/** CPUs in a cpuset list such as "0-3,8,10-11" (7), each counted once; null when it does not parse. */
export function cpusetCount(cpuset: string): number | null {
  const ranges: [number, number][] = [];
  for (const part of cpuset.split(",")) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!m) return null;
    const a = Number(m[1]), b = Number(m[2] ?? m[1]);
    if (a > b) return null;
    ranges.push([a, b]);
  }
  ranges.sort((x, y) => x[0] - y[0]);
  let count = 0, last = -1;
  for (const [a, b] of ranges) {
    if (b > last) count += b - Math.max(a, last + 1) + 1;
    last = Math.max(last, b);
  }
  return count;
}

export function pm2Owner(home: string): string {
  const m = /^\/home\/([^/]+)\/\.pm2\/?$/.exec(home);
  if (m) return m[1];
  if (/^\/root\/\.pm2\/?$/.test(home)) return "root";
  return home;
}

function shortModel(name: string): string {
  const s = name.replace(/\b(NVIDIA|GeForce|RTX|Tesla|Quadro)\b/gi, " ").replace(/\s+/g, " ").trim();
  return s || "GPU";
}

/** First word of the short model name ("A100-SXM4-80GB" -> "A100"), for the 96 px tile. */
function modelWord(name: string): string {
  return shortModel(name).split(/[\s-]+/).find(Boolean) ?? "GPU";
}

/** Network rate for the tile: B, K, M or G with one letter and at most one decimal ("2.0K", "512B"). */
function rate(v: number): string {
  if (!(v >= 1)) return "0B";
  const i = Math.min(3, Math.floor(Math.log(v) / Math.log(1024)));
  const x = v / 1024 ** i;
  return `${i === 0 ? x.toFixed(0) : x.toFixed(1)}${["B", "K", "M", "G"][i]}`;
}

/** Bytes for the tile: under 100 of a unit one decimal, otherwise none ("512 MB", "1.8 GB"). */
function compactBytes(b: number): string {
  if (!(b > 0)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (i < units.length - 1 && b >= 1024 ** (i + 1)) i++;
  const x = b / 1024 ** i;
  return `${x < 100 ? x.toFixed(1) : x.toFixed(0)} ${units[i]}`;
}

function joinNames(items: string[]): string {
  return items.join(" · ");
}

const gpuKey = (g: GpuMetrics) => `gpu:${g.uuid || g.index}`;

export function buildTopology(
  snapshot: MetricSnapshot,
  nowMs: number,
  thresholds: Record<MetricKey, Threshold>,
): TopologyGraph {
  const gpus = snapshot.gpu ?? [];
  const containers = [...(snapshot.containers ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const pm2Apps = [...(snapshot.pm2_apps ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  const gpuByIndex = new Map(gpus.map((g) => [g.index, g]));
  const isRunning = (c: ContainerInfo) => c.state === "running";
  const ctKey = (c: ContainerInfo) => `ct:${c.name}`;
  const pm2Key = (a: Pm2App) => `pm2:${a.home}:${a.name}`;

  const edges: TopoEdge[] = [];
  /** VRAM behind each GPU-use line, for the GPU tooltip's `Used by`. */
  const usedVram = new Map<TopoEdge, number>();
  const pushUse = (from: string, g: GpuMetrics, vram: number) => {
    const edge: TopoEdge = { from, to: gpuKey(g), kind: "gpu", label: `uses ${gb(vram)}` };
    edges.push(edge);
    usedVram.set(edge, vram);
  };

  // --- GPU use, per (workload, GPU) -------------------------------------
  for (const c of containers) {
    if (!isRunning(c)) continue;
    const perGpu = new Map<number, number>();
    for (const p of c.gpu?.processes ?? []) perGpu.set(p.gpu_index, (perGpu.get(p.gpu_index) ?? 0) + p.vram_bytes);
    for (const [index, vram] of perGpu) {
      const g = gpuByIndex.get(index);
      if (!g) continue;
      pushUse(ctKey(c), g, vram);
    }
  }

  const pm2ByPid = new Map<number, Pm2App>();
  for (const a of pm2Apps) for (const pid of a.pids) pm2ByPid.set(pid, a);
  const pm2Use = new Map<string, Map<GpuMetrics, number>>();
  const procs = new Map<number, { name?: string; user?: string; cmd?: string; vram: number; perGpu: Map<GpuMetrics, number> }>();
  for (const g of gpus) {
    for (const p of g.processes) {
      if (p.container) continue;
      const app = pm2ByPid.get(p.pid);
      if (app) {
        const m = pm2Use.get(pm2Key(app)) ?? new Map<GpuMetrics, number>();
        m.set(g, (m.get(g) ?? 0) + p.vram_bytes);
        pm2Use.set(pm2Key(app), m);
      } else {
        const e = procs.get(p.pid) ?? { name: p.name, user: p.user, cmd: p.cmd, vram: 0, perGpu: new Map<GpuMetrics, number>() };
        e.vram += p.vram_bytes;
        e.perGpu.set(g, (e.perGpu.get(g) ?? 0) + p.vram_bytes);
        procs.set(p.pid, e);
      }
    }
  }
  for (const a of pm2Apps) {
    for (const [g, vram] of pm2Use.get(pm2Key(a)) ?? []) {
      pushUse(pm2Key(a), g, vram);
    }
  }
  const procPids = [...procs.keys()].sort((a, b) => a - b);
  for (const pid of procPids) {
    for (const [g, vram] of procs.get(pid)!.perGpu) {
      pushUse(`proc:${pid}`, g, vram);
    }
  }

  // --- limits and reservations (only when the agent reported them) -----------
  for (const c of containers) {
    if (!isRunning(c) || !c.limits) continue;
    const used = new Set((c.gpu?.processes ?? []).map((p) => p.gpu_index));
    for (const index of c.gpu_reserved_indices ?? []) {
      const g = gpuByIndex.get(index);
      if (!g || used.has(index)) continue;
      edges.push({ from: ctKey(c), to: gpuKey(g), kind: "gpuReserved", label: "reserved · idle" });
    }
    const cpu: string[] = [];
    if (c.limits.cpus != null) cpu.push(`max ${num(c.limits.cpus)} cores`);
    if (c.limits.cpuset) cpu.push(`pinned ${c.limits.cpuset}`);
    if (cpu.length) edges.push({ from: ctKey(c), to: "hw:cpu", kind: "cpu", label: cpu.join(" · ") });
    const mem: string[] = [];
    if (c.limits.memory > 0) mem.push(`max ${formatBytes(c.limits.memory)}`);
    if (c.limits.memory_reservation > 0) mem.push(`reserved ${formatBytes(c.limits.memory_reservation)}`);
    if (mem.length) edges.push({ from: ctKey(c), to: "hw:mem", kind: "mem", label: mem.join(" · ") });
  }

  const nameOf = (key: string) => {
    const c = containers.find((x) => ctKey(x) === key);
    if (c) return c.name;
    const a = pm2Apps.find((x) => pm2Key(x) === key);
    if (a) return a.name;
    const pid = Number(key.slice("proc:".length));
    return procs.get(pid)?.name || `pid ${pid}`;
  };

  // --- nodes ----------------------------------------------------------------
  const nodes: TopoNode[] = [];
  const sev = (percent: number, key: "cpu" | "mem" | "disk"): Level => severityFor(percent, thresholds[key]);
  const { cpu, memory, disk } = snapshot;

  const limitsKnown = containers.some((c) => c.limits);
  const running = containers.filter(isRunning);
  const cpuTipRows: TipRow[] = [
    { label: "Usage", value: `${cpu.percent_total}%` },
    { label: "Cores", value: `${cpu.count_physical} physical / ${cpu.count_logical} logical` },
    ...(cpu.freq_current > 0 ? [{ label: "Clock", value: `${(cpu.freq_current / 1000).toFixed(1)} GHz` }] : []),
  ];
  const memTipRows: TipRow[] = [
    { label: "Usage", value: `${formatBytes(memory.used)} / ${formatBytes(memory.total)} (${memory.percent}%)` },
  ];
  if (limitsKnown) {
    // A CPU limit is `cpus` or a non-empty cpuset (§4.3); the sum counts `cpus`, else the cpuset's CPUs.
    const cpuLimited = (c: ContainerInfo) => !!c.limits && (c.limits.cpus != null || !!c.limits.cpuset);
    const cores = (c: ContainerInfo) => c.limits!.cpus ?? cpusetCount(c.limits!.cpuset) ?? 0;
    const withCpu = running.filter(cpuLimited).sort((a, b) => cores(b) - cores(a));
    const sumCpu = withCpu.reduce((s, c) => s + cores(c), 0);
    cpuTipRows.push({ label: "Container CPU limits", value: `${num(sumCpu)} / ${cpu.count_logical} cores`, tone: "cpu" });
    if (withCpu.length) {
      const share = (c: ContainerInfo) => c.limits!.cpus != null ? `${c.name} ${num(c.limits!.cpus)}` : `${c.name} pinned ${c.limits!.cpuset}`;
      cpuTipRows.push({ label: "Per container", value: joinNames(withCpu.map(share)) });
    }
    const noCpu = running.filter((c) => c.limits && !cpuLimited(c)).map((c) => c.name).sort();
    if (noCpu.length) cpuTipRows.push({ label: "No limit", value: joinNames(noCpu) });

    const withMem = running.filter((c) => (c.limits?.memory ?? 0) > 0).sort((a, b) => b.limits!.memory - a.limits!.memory);
    const withRes = running.filter((c) => (c.limits?.memory_reservation ?? 0) > 0).sort((a, b) => b.limits!.memory_reservation - a.limits!.memory_reservation);
    memTipRows.push({ label: "Container memory limits", value: formatPair(withMem.reduce((s, c) => s + c.limits!.memory, 0), memory.total), tone: "mem" });
    if (withMem.length) memTipRows.push({ label: "Per container", value: joinNames(withMem.map((c) => `${c.name} ${formatBytes(c.limits!.memory)}`)) });
    memTipRows.push({ label: "Reservations", value: formatPair(withRes.reduce((s, c) => s + c.limits!.memory_reservation, 0), memory.total), tone: "mem" });
    if (withRes.length) memTipRows.push({ label: "Reserved by", value: joinNames(withRes.map((c) => `${c.name} ${formatBytes(c.limits!.memory_reservation)}`)) });
    const noMem = running.filter((c) => c.limits && c.limits.memory === 0).map((c) => c.name).sort();
    if (noMem.length) memTipRows.push({ label: "No limit", value: joinNames(noMem) });
  }
  if (memory.swap_total > 0) {
    memTipRows.push({ label: "Swap", value: `${formatBytes(memory.swap_used)} / ${formatBytes(memory.swap_total)}` });
  }
  cpuTipRows.push({ value: "Per-core charts: System Monitor", tone: "hint" });

  nodes.push(
    {
      key: "hw:cpu", kind: "cpu", groupId: "hw", name: "CPU",
      sub: `${cpu.count_logical} cores`,
      badge: { text: `${Math.round(cpu.percent_total)}%`, level: sev(cpu.percent_total, "cpu") },
      meter: { percent: cpu.percent_total, level: sev(cpu.percent_total, "cpu") },
      persist: true, tip: { title: "CPU", rows: cpuTipRows },
    },
    {
      key: "hw:mem", kind: "mem", groupId: "hw", name: "Memory",
      sub: formatPair(memory.used, memory.total),
      badge: { text: `${Math.round(memory.percent)}%`, level: sev(memory.percent, "mem") },
      meter: { percent: memory.percent, level: sev(memory.percent, "mem") },
      persist: true, tip: { title: "Memory", rows: memTipRows },
    },
    {
      key: "hw:disk", kind: "disk", groupId: "hw", name: "Disk /",
      sub: formatPair(disk.used, disk.total),
      badge: { text: `${Math.round(disk.percent)}%`, level: sev(disk.percent, "disk") },
      meter: { percent: disk.percent, level: sev(disk.percent, "disk") },
      persist: true,
      tip: { title: "Disk /", rows: [{ label: "Usage", value: `${formatBytes(disk.used)} / ${formatBytes(disk.total)} (${disk.percent}%)` }] },
    },
    {
      key: "hw:net", kind: "net", groupId: "hw", name: "Network",
      sub: `↑ ${rate(snapshot.network?.rates.send_rate ?? 0)} · ↓ ${rate(snapshot.network?.rates.recv_rate ?? 0)}`,
      persist: true,
      tip: {
        title: "Network",
        rows: [
          { label: "Send", value: formatRate(snapshot.network?.rates.send_rate ?? 0) },
          { label: "Receive", value: formatRate(snapshot.network?.rates.recv_rate ?? 0) },
        ],
      },
    },
  );

  const sortedGpus = [...gpus].sort((a, b) => a.index - b.index);
  for (const g of sortedGpus) {
    const key = gpuKey(g);
    const users = edges.filter((e) => e.to === key && e.kind === "gpu");
    const reservers = edges.filter((e) => e.to === key && e.kind === "gpuReserved");
    const short = modelWord(g.name);
    const vramPct = g.mem_total > 0 ? (g.mem_used / g.mem_total) * 100 : 0;
    const sub = reservers.length > 0 && users.length === 0
      ? `${short} · reserved`
      : `${short} · ${Math.round(g.mem_used / GiB)}/${Math.round(g.mem_total / GiB)} GB`;
    const rows: TipRow[] = [
      { label: "Model", value: g.name },
      { label: "Utilisation", value: `${Math.round(g.gpu_util)}%`, tone: "gpu" },
      { label: "Temperature", value: `${g.temperature} °C` },
      { label: "VRAM", value: formatPair(g.mem_used, g.mem_total), tone: "gpu" },
    ];
    if (users.length) rows.push({ label: "Used by", value: joinNames(users.map((e) => `${nameOf(e.from)} ${gb(usedVram.get(e) ?? 0)}`)) });
    if (reservers.length) rows.push({ label: "Reserved, idle", value: joinNames(reservers.map((e) => nameOf(e.from))) });
    nodes.push({
      key, kind: "gpu", groupId: "gpu", name: `GPU ${g.index}`, sub,
      badge: { text: `${Math.round(g.gpu_util)}%`, level: g.gpu_util <= 0 ? "idle" : g.gpu_util >= 90 ? "warn" : "ok" },
      meter: { percent: vramPct, level: vramPct >= 90 ? "warn" : "ok" },
      persist: true, tip: { title: `GPU ${g.index}`, rows },
    });
  }

  for (const c of containers) {
    const isUp = isRunning(c);
    const finished = parseDockerTime(c.finished_at);
    const finishedAgo = finished != null && c.exit_code != null ? formatAge(nowMs - finished) : null;
    let sub: string;
    if (isUp) {
      if (c.limits && c.limits.memory > 0) sub = `RAM ${formatPair(c.mem_usage, c.limits.memory)}`;
      else if (c.limits) sub = `RAM ${compactBytes(c.mem_usage)} · ∞`;
      else sub = `RAM ${compactBytes(c.mem_usage)}`;
    } else if (finishedAgo != null) {
      sub = c.state === "exited" ? `exit ${c.exit_code} · ${finishedAgo} ago` : `${c.state} ${c.exit_code} · ${finishedAgo} ago`;
    } else {
      sub = c.state;
    }
    const cpuPct = Math.round(c.cpu_percent);
    const started = parseDockerTime(c.started_at);
    const rows: TipRow[] = [
      { label: "Image", value: c.image },
      {
        label: "State",
        value: isUp
          ? (started != null ? `running · ${formatAge(nowMs - started)}` : c.state)
          : finishedAgo != null ? `${c.state} (${c.exit_code}) · ${finishedAgo} ago` : c.state,
      },
      {
        label: "CPU",
        value: `${cpuPct}% · ` + (!c.limits ? "limit unknown"
          : [c.limits.cpus != null ? `max ${num(c.limits.cpus)} cores` : "", c.limits.cpuset ? `pinned ${c.limits.cpuset}` : ""].filter(Boolean).join(" · ") || "no limit"),
        tone: "cpu",
      },
      { label: "Memory", value: formatBytes(c.mem_usage), tone: "mem" },
    ];
    if (c.limits) {
      rows.push({ label: "Memory limit", value: c.limits.memory > 0 ? formatBytes(c.limits.memory) : "no limit" });
      if (c.limits.memory_reservation > 0) rows.push({ label: "Reservation", value: formatBytes(c.limits.memory_reservation) });
    }
    for (const e of edges.filter((x) => x.from === ctKey(c) && (x.kind === "gpu" || x.kind === "gpuReserved"))) {
      const g = gpus.find((x) => gpuKey(x) === e.to);
      rows.push({ label: g ? `GPU ${g.index}` : "GPU", value: e.label, tone: "gpu" });
    }
    if (c.ports.length) rows.push({ label: "Ports", value: c.ports.join(", ") });
    nodes.push({
      key: ctKey(c), kind: "container", groupId: c.compose_project ? `compose:${c.compose_project}` : "solo",
      name: c.name, sub,
      ...(isUp ? { badge: { text: `${cpuPct}%`, level: (cpuPct === 0 ? "idle" : "ok") as Level } } : {}),
      state: isUp ? "running" : "stopped", persist: true, tip: { title: c.name, rows },
    });
  }

  for (const a of pm2Apps) {
    const cpuPct = Math.round(a.cpu_percent);
    nodes.push({
      key: pm2Key(a), kind: "pm2", groupId: `pm2:${a.home}`,
      name: a.name + (a.instances > 1 ? ` ×${a.instances}` : ""),
      sub: `RAM ${compactBytes(a.mem_bytes)}`,
      badge: { text: `${cpuPct}%`, level: cpuPct === 0 ? "idle" : "ok" },
      persist: true,
      tip: {
        title: a.name,
        rows: [
          { value: "PM2 app" },
          { label: "Instances", value: String(a.instances) },
          { label: "PM2 home", value: a.home },
          { label: "CPU", value: `${a.cpu_percent.toFixed(1)}%`, tone: "cpu" },
          { label: "RSS", value: formatBytes(a.mem_bytes), tone: "mem" },
        ],
      },
    });
  }

  for (const pid of procPids) {
    const p = procs.get(pid)!;
    const info = (snapshot.processes ?? []).find((x) => x.pid === pid);
    const rows: TipRow[] = [
      { label: "PID", value: String(pid) },
      { label: "User", value: p.user || "?" },
    ];
    if (p.cmd) rows.push({ label: "Command", value: p.cmd });
    if (info) rows.push({ label: "Usage", value: `CPU ${Math.round(info.cpu)}% · memory ${info.mem}%` });
    for (const [g, vram] of p.perGpu) rows.push({ label: `GPU ${g.index}`, value: gb(vram), tone: "gpu" });
    nodes.push({
      key: `proc:${pid}`, kind: "proc", groupId: "other", name: p.name || `pid ${pid}`,
      sub: `${p.user || "?"} · ${gb(p.vram)}`,
      ...(info ? { badge: { text: `${Math.round(info.cpu)}%`, level: (Math.round(info.cpu) === 0 ? "idle" : "ok") as Level } } : {}),
      persist: false,
      tip: { title: p.name || `pid ${pid}`, rows },
    });
  }

  // --- groups (layout order; empty ones omitted) -------------------------------
  const groups: TopoGroup[] = [{ id: "hw", kind: "lane", label: "Hardware", row: 0 }];
  if (gpus.length) groups.push({ id: "gpu", kind: "lane", label: `GPU · ${gpus.length}`, row: 0 });
  const projects = [...new Set(containers.map((c) => c.compose_project).filter((p): p is string => !!p))].sort();
  for (const p of projects) groups.push({ id: `compose:${p}`, kind: "compose", label: p, tag: "compose", row: 1 });
  if (containers.some((c) => !c.compose_project)) groups.push({ id: "solo", kind: "solo", label: "Standalone containers", row: 1 });
  for (const home of [...new Set(pm2Apps.map((a) => a.home))].sort()) {
    groups.push({ id: `pm2:${home}`, kind: "host", label: `PM2 · ${pm2Owner(home)}`, tag: "outside containers", row: 2 });
  }
  if (procPids.length) groups.push({ id: "other", kind: "host", label: "Other processes", tag: "outside containers", row: 2, underGpu: true });

  // --- status ----------------------------------------------------------------
  const outdatedAgent = (containers.length > 0 && !containers.some((c) => c.limits)) || snapshot.pm2_apps === undefined;
  const parts = [`${cpu.count_logical} cores`, `${formatBytes(memory.total)} memory`, `${formatBytes(disk.total)} disk`];
  if (gpus.length) parts.push(`${gpus.length} ${gpus.length === 1 ? "GPU" : "GPUs"}`);
  if (containers.length) parts.push(`${containers.length} ${containers.length === 1 ? "container" : "containers"} (${running.length} running)`);
  const outside = pm2Apps.length + procPids.length;
  if (outside > 0) parts.push(`${outside} outside ${outside === 1 ? "container" : "containers"}`);

  return { nodes, groups, edges, summary: parts.join(" · "), outdatedAgent };
}
