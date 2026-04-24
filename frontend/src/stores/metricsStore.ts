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

export interface GpuProcess {
  pid: number;
  vram_bytes: number;
}

export interface GpuMetrics {
  index: number;
  name: string;
  uuid: string;
  driver_version: string;
  gpu_util: number;
  mem_util: number;
  mem_total: number;
  mem_used: number;
  temperature: number;
  power_watts: number;
  power_limit_watts: number;
  clock_sm_mhz: number;
  clock_mem_mhz: number;
  fan_speed: number;
  processes: GpuProcess[];
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

interface AgentData {
  current: MetricSnapshot | null;
  history: MetricSnapshot[];
}

const MAX_HISTORY = 120;

interface MetricsStore {
  // Multi-agent data
  agents: Record<string, AgentData>;
  agentIds: string[];
  selectedAgentId: string | null;
  connected: boolean;

  // Derived — current agent's data
  current: MetricSnapshot | null;
  history: MetricSnapshot[];
  agentId: string | null;

  pushMetrics: (agentId: string, data: MetricSnapshot) => void;
  setConnected: (value: boolean) => void;
  selectAgent: (agentId: string) => void;
  loadHistory: (agentId: string, data: MetricSnapshot[]) => void;
}

function deriveSelected(agents: Record<string, AgentData>, selectedId: string | null) {
  const id = selectedId && agents[selectedId] ? selectedId : Object.keys(agents)[0] ?? null;
  const agent = id ? agents[id] : null;
  return {
    agentId: id,
    current: agent?.current ?? null,
    history: agent?.history ?? [],
  };
}

export const useMetricsStore = create<MetricsStore>((set) => ({
  agents: {},
  agentIds: [],
  selectedAgentId: null,
  connected: false,
  current: null,
  history: [],
  agentId: null,

  pushMetrics: (agentId, data) => {
    set((state) => {
      const existing = state.agents[agentId] ?? { current: null, history: [] };
      const history = [...existing.history, data].slice(-MAX_HISTORY);
      const agents = { ...state.agents, [agentId]: { current: data, history } };
      const agentIds = Object.keys(agents);
      const selectedAgentId = state.selectedAgentId ?? agentId;
      return { agents, agentIds, selectedAgentId, ...deriveSelected(agents, selectedAgentId) };
    });
  },

  setConnected: (value) => set({ connected: value }),

  selectAgent: (agentId) => {
    set((state) => ({
      selectedAgentId: agentId,
      ...deriveSelected(state.agents, agentId),
    }));
  },

  loadHistory: (agentId, data) => {
    set((state) => {
      const agents = {
        ...state.agents,
        [agentId]: {
          current: data.length > 0 ? data[data.length - 1] : null,
          history: data.slice(-MAX_HISTORY),
        },
      };
      const agentIds = Object.keys(agents);
      const selectedAgentId = state.selectedAgentId ?? agentId;
      return { agents, agentIds, selectedAgentId, ...deriveSelected(agents, selectedAgentId) };
    });
  },
}));
