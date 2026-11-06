import type { ContainerInfo, GpuMetrics, GpuProcess, MetricSnapshot, Pm2App } from "../../../stores/metricsStore";
import { makeContainer, makeMetricSnapshot } from "../../../test/fixtures";

export const MiB = 1024 ** 2;
export const GiB = 1024 ** 3;
export const NOW = Date.parse("2026-11-05T12:00:00Z");

export function gpu(index: number, overrides: Partial<GpuMetrics> = {}): GpuMetrics {
  return {
    index,
    name: "NVIDIA RTX A6000",
    uuid: `GPU-${index}`,
    driver_version: "550.00",
    gpu_util: 0,
    mem_util: 0,
    mem_total: 48 * GiB,
    mem_used: 0,
    temperature: 40,
    power_watts: 30,
    power_limit_watts: 300,
    clock_sm_mhz: 210,
    clock_mem_mhz: 405,
    fan_speed: 30,
    processes: [],
    ...overrides,
  };
}

/** A small host: 128 logical cores at 2.9 GHz, 23 % CPU, memory 206/503 GiB, one GPU, no workloads. */
export function base(overrides: Partial<MetricSnapshot> = {}): MetricSnapshot {
  return makeMetricSnapshot({
    cpu: {
      percent_total: 23,
      percent_per_core: [23],
      count_logical: 128,
      count_physical: 64,
      freq_current: 2900,
      freq_max: 3500,
    },
    memory: {
      total: 503 * GiB,
      available: 297 * GiB,
      used: 206 * GiB,
      percent: 41,
      swap_total: 0,
      swap_used: 0,
      swap_percent: 0,
    },
    disk: { total: 2000 * GiB, used: 500 * GiB, free: 1500 * GiB, percent: 25, read_bytes: 0, write_bytes: 0 },
    network: {
      io: {},
      rates: { send_rate: 2048, recv_rate: 1024 },
      interfaces: [],
      connections: [],
      connection_count: 0,
    },
    gpu: [gpu(0)],
    containers: [],
    pm2_apps: [],
    timestamp: NOW / 1000,
    ...overrides,
  });
}

const NEW_FIELDS = ["limits", "started_at", "finished_at", "exit_code", "gpu_reserved_indices"] as const;

function ctr(name: string, overrides: Partial<ContainerInfo> = {}): ContainerInfo {
  return makeContainer({
    id: `id-${name}`,
    name,
    image: `img/${name}:latest`,
    started_at: "2026-11-03T09:12:44.123456789Z",
    finished_at: "",
    exit_code: 0,
    gpu_reserved_indices: [],
    limits: { cpus: null, cpuset: "", memory: 0, memory_reservation: 0 },
    ...overrides,
  });
}

function gpuUse(pids: { pid: number; vram_bytes: number; gpu_index: number }[]): ContainerInfo["gpu"] {
  return { vram_bytes: pids.reduce((s, p) => s + p.vram_bytes, 0), processes: pids };
}

/** A running container with one GPU process on `index` (which may not exist in the snapshot). */
export function withProcessOnGpu(index: number): MetricSnapshot {
  const proc = { pid: 5000, vram_bytes: 2 * GiB, gpu_index: index };
  return base({
    containers: [ctr("ghost", { gpu: gpuUse([proc]) })],
  });
}

export interface FullOptions {
  /** Drop every field a pre-Topology agent does not send. */
  stripNewFields?: boolean;
  /** Snapshot timestamp in seconds (default: NOW). */
  timestamp?: number;
  /** Add one running standalone container (no compose project) of this name. */
  extraContainer?: string;
  /** CPU usage in percent (default: 23). */
  cpuPercent?: number;
  /** Add one more outside-container GPU process (on GPU 2) with this pid. */
  extraProcess?: number;
}

/** The reference host used by the topology tests. */
export function full(opts: FullOptions = {}): MetricSnapshot {
  const vllmProcs = [
    { pid: 3001, vram_bytes: 20 * GiB, gpu_index: 0 },
    { pid: 3002, vram_bytes: 18.2 * GiB, gpu_index: 0 },
  ];
  const containers: ContainerInfo[] = [
    ctr("api", { compose_project: "shop", compose_service: "api", mem_usage: 1.8 * GiB, limits: { cpus: 2, cpuset: "", memory: 4 * GiB, memory_reservation: 2 * GiB } }),
    ctr("redis", { compose_project: "shop", compose_service: "redis" }),
    ctr("rabbitmq", { compose_project: "shop", compose_service: "rabbitmq", limits: { cpus: 1, cpuset: "", memory: 0, memory_reservation: 0 } }),
    ctr("batch", {
      compose_project: "shop",
      compose_service: "batch",
      state: "exited",
      status: "Exited (0) 3 hours ago",
      cpu_percent: 0,
      finished_at: "2026-11-05T09:00:00Z",
      exit_code: 0,
      limits: { cpus: 4, cpuset: "", memory: 8 * GiB, memory_reservation: 0 },
    }),
    ctr("vllm", {
      compose_project: "llm",
      compose_service: "vllm",
      gpu: gpuUse(vllmProcs),
      limits: { cpus: 8, cpuset: "", memory: 64 * GiB, memory_reservation: 32 * GiB },
      gpu_reserved_indices: [0],
    }),
    ctr("open-webui", { compose_project: "llm", compose_service: "open-webui" }),
    ctr("train", { limits: { cpus: 16, cpuset: "0-15", memory: 0, memory_reservation: 0 } }),
    ctr("jupyter", { limits: { cpus: null, cpuset: "", memory: 32 * GiB, memory_reservation: 0 }, gpu_reserved_indices: [1] }),
    ctr("idle", { state: "created", status: "Created", cpu_percent: 0, started_at: "", finished_at: "", exit_code: null }),
  ];
  if (opts.extraContainer !== undefined) containers.push(ctr(opts.extraContainer));
  const gpuProc = (pid: number, vram: number, extra: Partial<GpuProcess> = {}): GpuProcess => ({ pid, vram_bytes: vram, ...extra });
  const gpus = [
    gpu(0, {
      gpu_util: 60,
      mem_used: 39.2 * GiB,
      processes: [
        gpuProc(3001, 20 * GiB, { name: "vllm", container: "vllm" }),
        gpuProc(3002, 18.2 * GiB, { name: "vllm", container: "vllm" }),
        gpuProc(200, 1 * GiB, { name: "node", user: "hspark" }),
      ],
    }),
    gpu(1),
    gpu(2, opts.extraProcess === undefined ? {} : {
      processes: [gpuProc(opts.extraProcess, 2 * GiB, { name: "worker", user: "hspark" })],
    }),
    gpu(3, { gpu_util: 5, mem_used: 18.4 * GiB, processes: [gpuProc(900, 18.4 * GiB, { name: "python", user: "hspark", cmd: "python train.py --fold 3" })] }),
  ];
  const pm2: Pm2App[] = [
    { name: "admin-api", home: "/home/hspark/.pm2", instances: 1, pids: [200], cpu_percent: 3.1, mem_bytes: 220200960 },
  ];
  const snap = base({
    gpu: gpus,
    containers,
    pm2_apps: pm2,
    processes: [{ pid: 900, name: "python", cpu: 98, mem: 1.8, user: "hspark", status: "running", started: 0 }],
    ...(opts.timestamp !== undefined ? { timestamp: opts.timestamp } : {}),
  });
  if (opts.cpuPercent !== undefined) snap.cpu.percent_total = opts.cpuPercent;
  if (opts.stripNewFields) {
    delete snap.pm2_apps;
    snap.containers = snap.containers!.map((c) => {
      const copy: Record<string, unknown> = { ...c };
      for (const k of NEW_FIELDS) delete copy[k];
      return copy as unknown as ContainerInfo;
    });
  }
  return snap;
}
