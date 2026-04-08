import { create } from "zustand";

export interface CpuMetrics {
  percent_total: number;
  percent_per_core: number[];
  count_logical: number;
  count_physical: number;
  freq_current: number;
  freq_max: number;
}

export interface MemoryMetrics {
  total: number;
  available: number;
  used: number;
  percent: number;
  swap_total: number;
  swap_used: number;
  swap_percent: number;
}

export interface DiskMetrics {
  total: number;
  used: number;
  free: number;
  percent: number;
  read_bytes: number;
  write_bytes: number;
}

export interface GpuMetrics {
  index: number;
  name: string;
  gpu_util: number;
  mem_util: number;
  mem_total: number;
  mem_used: number;
  temperature: number;
  power_watts: number;
}

export interface ContainerInfo {
  id: string;
  name: string;
  image: string;
  status: string;
  state: string;
  cpu_percent: number;
  mem_usage: number;
  mem_limit: number;
  ports: string[];
}

export interface NetworkConnection {
  type: string;
  laddr: string;
  raddr: string;
  status: string;
  pid: number | null;
}

export interface NetworkInterface {
  name: string;
  ip: string;
  is_up: boolean;
  speed: number;
}

export interface NetworkMetrics {
  io: Record<string, number>;
  rates: { send_rate: number; recv_rate: number };
  connections: NetworkConnection[];
  interfaces: NetworkInterface[];
  connection_count: number;
}

export interface ProcessInfo {
  pid: number;
  name: string;
  cpu: number;
  mem: number;
  user: string;
  status: string;
  started: number;
}

export interface MetricSnapshot {
  cpu: CpuMetrics;
  memory: MemoryMetrics;
  disk: DiskMetrics;
  gpu?: GpuMetrics[];
  containers?: ContainerInfo[];
  network?: NetworkMetrics;
  processes?: ProcessInfo[];
  timestamp: number;
}

const MAX_HISTORY = 120; // 2 minutes at 1s interval

interface MetricsStore {
  current: MetricSnapshot | null;
  history: MetricSnapshot[];
  agentId: string | null;
  connected: boolean;

  pushMetrics: (agentId: string, data: MetricSnapshot) => void;
  setConnected: (value: boolean) => void;
  loadHistory: (agentId: string, data: MetricSnapshot[]) => void;
}

export const useMetricsStore = create<MetricsStore>((set) => ({
  current: null,
  history: [],
  agentId: null,
  connected: false,

  pushMetrics: (agentId, data) => {
    set((state) => {
      const history = [...state.history, data].slice(-MAX_HISTORY);
      return { current: data, history, agentId };
    });
  },

  setConnected: (value) => set({ connected: value }),

  loadHistory: (agentId, data) => {
    set({
      agentId,
      history: data.slice(-MAX_HISTORY),
      current: data.length > 0 ? data[data.length - 1] : null,
    });
  },
}));
