import { useId, useMemo, useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, ResponsiveContainer, Tooltip,
} from "recharts";
import { useMetricsStore, type GpuMetrics } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";
import { useEffect } from "react";

type Tab = "overview" | "detail" | "processes";
type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function GpuCard({ gpu, selected, onClick }: { gpu: GpuMetrics; selected: boolean; onClick: () => void }) {
  return (
    <button className={`gpu-card ${selected ? "gpu-card-selected" : ""}`} onClick={onClick}>
      <div className="gpu-card-header">
        <span className="gpu-card-index">GPU {gpu.index}</span>
        <span className="gpu-card-name">{gpu.name}</span>
      </div>
      <div className="gpu-card-metrics">
        <div className="gpu-card-row">
          <span className="gpu-card-label">Util</span>
          <div className="gpu-card-bar-track">
            <div className="gpu-card-bar" style={{ width: `${gpu.gpu_util}%`, background: "var(--color-gpu)" }} />
          </div>
          <span className="gpu-card-val">{gpu.gpu_util}%</span>
        </div>
        <div className="gpu-card-row">
          <span className="gpu-card-label">VRAM</span>
          <div className="gpu-card-bar-track">
            <div className="gpu-card-bar" style={{ width: `${gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total * 100) : 0}%`, background: "var(--color-accent)" }} />
          </div>
          <span className="gpu-card-val">{formatBytes(gpu.mem_used)}/{formatBytes(gpu.mem_total)}</span>
        </div>
        <div className="gpu-card-detail">
          <span>{gpu.temperature}°C</span>
          <span>{gpu.power_watts > 0 ? `${gpu.power_watts.toFixed(0)}W/${gpu.power_limit_watts.toFixed(0)}W` : "N/A"}</span>
          <span>Fan {gpu.fan_speed}%</span>
          <span>{gpu.clock_sm_mhz}MHz</span>
        </div>
      </div>
    </button>
  );
}

function useHistoricalGpu(agentId: string | null, range: TimeRange) {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    if (!agentId || range === "live") { setData([]); return; }
    fetchWithAuth(`/api/metrics/${agentId}/range?duration=${range}`)
      .then((r) => r.json())
      .then((d) => setData(d.metrics || []))
      .catch(() => setData([]));
  }, [agentId, range]);
  return data;
}

export default function GpuMonitor() {
  const current = useMetricsStore((s) => s.current);
  const history = useMetricsStore((s) => s.history);
  const connected = useMetricsStore((s) => s.connected);
  const agentId = useMetricsStore((s) => s.agentId);
  const [tab, setTab] = useState<Tab>("overview");
  const [timeRange, setTimeRange] = useState<TimeRange>("live");
  const [selectedGpu, setSelectedGpu] = useState(0);

  const historicalData = useHistoricalGpu(agentId, timeRange);
  const activeHistory = timeRange === "live" ? history : historicalData;

  const gpus = current?.gpu ?? [];

  if (!connected || gpus.length === 0) {
    return (
      <div className="gpu-empty">
        <p className="gpu-empty-title">{connected ? "No GPU detected" : "Connecting to server..."}</p>
        <p className="gpu-empty-sub">NVIDIA GPU with pynvml required. Set GLASSOPS_ENABLE_GPU=true</p>
      </div>
    );
  }

  return (
    <div className="gpu-monitor">
      <div className="gpu-controls">
        <div className="sysmon-tabs">
          {(["overview", "detail", "processes"] as Tab[]).map((t) => (
            <button key={t} className={`sysmon-tab ${tab === t ? "sysmon-tab-active" : ""}`}
              onClick={() => setTab(t)}>
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        {tab === "detail" && (
          <div className="sysmon-tabs">
            {(["live", "5m", "1h", "6h", "24h", "7d"] as TimeRange[]).map((r) => (
              <button key={r} className={`sysmon-tab ${timeRange === r ? "sysmon-tab-active" : ""}`}
                onClick={() => setTimeRange(r)}>
                {r === "live" ? "Live" : r}
              </button>
            ))}
          </div>
        )}
      </div>

      {tab === "overview" && (
        <div className="gpu-grid">
          {gpus.map((gpu) => (
            <GpuCard key={gpu.index} gpu={gpu} selected={selectedGpu === gpu.index}
              onClick={() => { setSelectedGpu(gpu.index); setTab("detail"); }} />
          ))}
        </div>
      )}

      {tab === "detail" && (
        <GpuDetailCharts history={activeHistory} gpuIndex={selectedGpu} timeRange={timeRange}
          gpuCount={gpus.length} onSelectGpu={setSelectedGpu} />
      )}

      {tab === "processes" && (
        <GpuProcesses gpus={gpus} />
      )}

      {gpus[0]?.driver_version && (
        <div className="gpu-footer">Driver: {gpus[0].driver_version}</div>
      )}
    </div>
  );
}

function GpuDetailCharts({ history, gpuIndex, timeRange, gpuCount, onSelectGpu }: {
  history: any[]; gpuIndex: number; timeRange: TimeRange; gpuCount: number; onSelectGpu: (i: number) => void;
}) {
  const gradId = useId();

  const data = useMemo(() =>
    history.map((m: any) => {
      const gpu = (m.gpu ?? [])[gpuIndex];
      return {
        t: m.timestamp ?? 0,
        util: gpu?.gpu_util ?? 0,
        mem: gpu?.mem_util ?? 0,
        temp: gpu?.temperature ?? 0,
        power: gpu?.power_watts ?? 0,
      };
    }), [history, gpuIndex]);

  const formatTime = (ts: number) => {
    const d = new Date(ts * 1000);
    if (timeRange === "live" || timeRange === "5m" || timeRange === "1h")
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (timeRange === "6h" || timeRange === "24h")
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit" });
  };

  const charts = [
    { key: "util", label: "Utilization %", color: "var(--color-gpu)", domain: [0, 100] },
    { key: "mem", label: "Memory %", color: "var(--color-accent)", domain: [0, 100] },
    { key: "temp", label: "Temperature °C", color: "var(--color-warning)", domain: [0, "auto"] },
    { key: "power", label: "Power W", color: "var(--color-danger)", domain: [0, "auto"] },
  ];

  return (
    <div className="gpu-detail">
      {gpuCount > 1 && (
        <div className="sysmon-tabs" style={{ marginBottom: 8 }}>
          {Array.from({ length: gpuCount }, (_, i) => (
            <button key={i} className={`sysmon-tab ${gpuIndex === i ? "sysmon-tab-active" : ""}`}
              onClick={() => onSelectGpu(i)}>GPU {i}</button>
          ))}
        </div>
      )}
      <div className="gpu-detail-grid">
        {charts.map((c) => (
          <div key={c.key} className="gpu-detail-chart">
            <span className="sysmon-chart-title">{c.label}</span>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 16, left: 4 }}>
                <defs>
                  <linearGradient id={`${gradId}-${c.key}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={c.color} stopOpacity={0.3} />
                    <stop offset="100%" stopColor={c.color} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="t" tick={{ fontSize: 9, fill: "var(--text-secondary)" }}
                  tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={60}
                  tickFormatter={formatTime} />
                <YAxis domain={c.domain as [number, number | string]} hide />
                <Tooltip
                  contentStyle={{ background: "rgba(20,20,40,0.9)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 8, fontSize: 11, color: "#e0e0e0" }}
                  formatter={(v) => [Number(v).toFixed(1)]}
                  labelFormatter={(ts) => formatTime(Number(ts))}
                />
                <Area type="monotone" dataKey={c.key} stroke={c.color} strokeWidth={1.5}
                  fill={`url(#${gradId}-${c.key})`} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ))}
      </div>
    </div>
  );
}

function GpuProcesses({ gpus }: { gpus: GpuMetrics[] }) {
  const allProcs = gpus.flatMap((gpu) =>
    (gpu.processes ?? []).map((p) => ({ ...p, gpuIndex: gpu.index, gpuName: gpu.name }))
  );

  if (allProcs.length === 0) {
    return <div className="gpu-empty"><p>No GPU processes running</p></div>;
  }

  return (
    <table className="docker-table">
      <thead>
        <tr><th>PID</th><th>GPU</th><th>VRAM Used</th></tr>
      </thead>
      <tbody>
        {allProcs.sort((a, b) => b.vram_bytes - a.vram_bytes).map((p) => (
          <tr key={`${p.gpuIndex}-${p.pid}`}>
            <td className="proc-cell-pid">{p.pid}</td>
            <td>GPU {p.gpuIndex}</td>
            <td className="docker-cell-num">{p.vram_bytes >= 0 ? formatBytes(p.vram_bytes) : "N/A"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
