import { useEffect, useId, useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
  Legend,
} from "recharts";
import { useMetricsStore } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";

type Tab = "overview" | "cores" | "processes";
type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";

const CORE_COLORS = [
  "#4facfe", "#43e97b", "#f7971e", "#f85032", "#a18cd1",
  "#38f9d7", "#fccb90", "#e0c3fc", "#667eea", "#764ba2",
  "#63e6be", "#ffa94d", "#ff6b6b", "#da77f2", "#20c997", "#fab005",
];
const PROCESS_COLORS = ["#4facfe", "#43e97b", "#f7971e", "#a18cd1", "#f85032"];

const TOOLTIP_STYLE = {
  background: "rgba(20,20,40,0.9)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8,
  fontSize: 11,
  color: "#e0e0e0",
};

function formatTime(ts: number, range: TimeRange): string {
  const d = new Date(ts * 1000);
  if (range === "live" || range === "5m" || range === "1h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  if (range === "6h" || range === "24h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function Gauge({ label, value, color, detail }: {
  label: string; value: number; color: string; detail?: string;
}) {
  const v = Math.min(100, Math.max(0, value));
  return (
    <div className="sysmon-gauge">
      <div className="sysmon-gauge-header">
        <span className="sysmon-gauge-label">{label}</span>
        <span className="sysmon-gauge-value" style={{ color }}>{v.toFixed(1)}%</span>
      </div>
      <div className="sysmon-gauge-track">
        <div className="sysmon-gauge-fill" style={{ width: `${v}%`, background: color }} />
      </div>
      {detail && <span className="sysmon-gauge-detail">{detail}</span>}
    </div>
  );
}

function TimeAxis({ range }: { range: TimeRange }) {
  return (
    <XAxis
      dataKey="t"
      tick={{ fontSize: 9, fill: "var(--text-secondary)" }}
      tickLine={false}
      axisLine={false}
      interval="preserveStartEnd"
      minTickGap={60}
      tickFormatter={(ts) => formatTime(Number(ts), range)}
    />
  );
}

function MiniChart({ data, dataKey, color, range }: {
  data: { t: number; value: number }[]; dataKey: string; color: string; range: TimeRange;
}) {
  const gradId = useId();
  // Auto-scale: max of data or 10%, whichever is higher, with 20% headroom
  const maxVal = Math.max(...data.map((d) => d.value), 1);
  const yMax = Math.min(100, Math.max(10, maxVal * 1.3));

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 16, left: 4 }}>
        <defs>
          <linearGradient id={`${gradId}-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <TimeAxis range={range} />
        <YAxis domain={[0, yMax]} hide />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v) => [`${Number(v).toFixed(1)}%`]}
          labelFormatter={(ts) => formatTime(Number(ts), range)}
        />
        <Area type="monotone" dataKey="value" stroke={color} strokeWidth={1.5}
          fill={`url(#${gradId}-${dataKey})`} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function CoresChart({ history, range }: { history: any[]; range: TimeRange }) {
  const coreCount = history[0]?.cpu?.percent_per_core?.length ?? 0;
  const data = useMemo(() =>
    history.map((m: any) => {
      const entry: Record<string, number> = { t: m.timestamp ?? 0 };
      (m.cpu?.percent_per_core ?? []).forEach((v: number, ci: number) => { entry[`c${ci}`] = v; });
      return entry;
    }), [history]);

  if (coreCount === 0) return <p className="sysmon-empty-sub">No core data</p>;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 20, left: 8 }}>
        <TimeAxis range={range} />
        <YAxis domain={[0, 100]} hide />
        <Tooltip contentStyle={TOOLTIP_STYLE}
          formatter={(v, name) => [`${Number(v).toFixed(1)}%`, `Core ${String(name).slice(1)}`]}
          labelFormatter={(ts) => formatTime(Number(ts), range)}
        />
        <Legend wrapperStyle={{ fontSize: 10 }} formatter={(v) => `C${v.slice(1)}`} />
        {Array.from({ length: coreCount }, (_, i) => (
          <Area key={i} type="monotone" dataKey={`c${i}`}
            stroke={CORE_COLORS[i % CORE_COLORS.length]} strokeWidth={1}
            fill="transparent" isAnimationActive={false} />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

function ProcessCpuChart({ history, range }: { history: any[]; range: TimeRange }) {
  const topNames = useMemo(() => {
    const cpuSum: Record<string, number> = {};
    const cpuCount: Record<string, number> = {};
    for (const m of history) {
      for (const p of m.processes ?? []) {
        cpuSum[p.name] = (cpuSum[p.name] ?? 0) + p.cpu;
        cpuCount[p.name] = (cpuCount[p.name] ?? 0) + 1;
      }
    }
    return Object.entries(cpuSum)
      .map(([name, sum]) => ({ name, avg: sum / (cpuCount[name] || 1) }))
      .sort((a, b) => b.avg - a.avg)
      .slice(0, 5)
      .map((e) => e.name);
  }, [history]);

  const data = useMemo(() =>
    history.map((m: any) => {
      const entry: Record<string, number> = { t: m.timestamp ?? 0 };
      for (const name of topNames) {
        const proc = (m.processes ?? []).find((p: any) => p.name === name);
        entry[name] = proc?.cpu ?? 0;
      }
      return entry;
    }), [history, topNames]);

  if (topNames.length === 0) return <p className="sysmon-empty-sub">No process data</p>;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 20, left: 8 }}>
        <TimeAxis range={range} />
        <YAxis hide />
        <Tooltip contentStyle={TOOLTIP_STYLE}
          formatter={(v) => [`${Number(v).toFixed(1)}%`]}
          labelFormatter={(ts) => formatTime(Number(ts), range)}
        />
        <Legend wrapperStyle={{ fontSize: 10 }} />
        {topNames.map((name, i) => (
          <Area key={name} type="monotone" dataKey={name}
            stroke={PROCESS_COLORS[i]} strokeWidth={1.5}
            fill="transparent" isAnimationActive={false} />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

function useHistoricalData(agentId: string | null, range: TimeRange) {
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!agentId || range === "live") { setData([]); return; }
    setLoading(true);
    fetchWithAuth(`/api/metrics/${agentId}/range?duration=${range}`)
      .then((r) => r.json())
      .then((d) => setData(d.metrics || []))
      .catch(() => setData([]))
      .finally(() => setLoading(false));
  }, [agentId, range]);

  return { data, loading };
}

/* ── Main ── */
export default function SystemMonitor() {
  const current = useMetricsStore((s) => s.current);
  const history = useMetricsStore((s) => s.history);
  const connected = useMetricsStore((s) => s.connected);
  const agentId = useMetricsStore((s) => s.agentId);
  const [tab, setTab] = useState<Tab>("overview");
  const [timeRange, setTimeRange] = useState<TimeRange>("live");

  const { data: historicalData, loading: histLoading } = useHistoricalData(agentId, timeRange);
  const activeHistory = timeRange === "live" ? history : historicalData;

  const chartData = useMemo(() =>
    activeHistory.map((m: any) => ({
      t: m.timestamp ?? 0,
      cpu: m.cpu?.percent_total ?? 0,
      mem: m.memory?.percent ?? 0,
      disk: m.disk?.percent ?? 0,
      gpu: m.gpu?.[0]?.gpu_util ?? 0,
    })), [activeHistory]);

  if (!connected || !current) {
    return (
      <div className="sysmon-empty">
        <p className="sysmon-empty-title">{connected ? "Waiting for data..." : "Connecting to server..."}</p>
        <p className="sysmon-empty-sub">Ensure the GlassOps Agent is running on your server.</p>
      </div>
    );
  }

  const { cpu, memory, disk, gpu } = current;
  const hasGpu = gpu && gpu.length > 0;

  return (
    <div className="sysmon">
      <div className="sysmon-gauges">
        <Gauge label="CPU" value={cpu.percent_total} color="var(--color-accent)"
          detail={`${cpu.count_physical}C/${cpu.count_logical}T · ${cpu.freq_current.toFixed(0)} MHz`} />
        <Gauge label="Memory" value={memory.percent} color="var(--color-success)"
          detail={`${formatBytes(memory.used)} / ${formatBytes(memory.total)}`} />
        <Gauge label="Disk" value={disk.percent} color="var(--color-warning)"
          detail={`${formatBytes(disk.used)} / ${formatBytes(disk.total)}`} />
        {hasGpu && (
          <Gauge label={`GPU ${gpu[0].name}`} value={gpu[0].gpu_util} color="var(--color-gpu)"
            detail={`${gpu[0].temperature}°C · ${gpu[0].power_watts.toFixed(0)}W`} />
        )}
      </div>

      <div className="sysmon-controls">
        <div className="sysmon-tabs">
          {(["overview", "cores", "processes"] as Tab[]).map((t) => (
            <button key={t} className={`sysmon-tab ${tab === t ? "sysmon-tab-active" : ""}`}
              onClick={() => setTab(t)}>
              {t === "overview" ? "Overview" : t === "cores" ? `Cores (${cpu.count_logical})` : "Process CPU"}
            </button>
          ))}
        </div>
        <div className="sysmon-tabs">
          {(["live", "5m", "1h", "6h", "24h", "7d"] as TimeRange[]).map((r) => (
            <button key={r} className={`sysmon-tab ${timeRange === r ? "sysmon-tab-active" : ""}`}
              onClick={() => setTimeRange(r)}>
              {r === "live" ? "Live" : r}
            </button>
          ))}
        </div>
      </div>

      {histLoading && <p className="sysmon-loading">Loading...</p>}

      {tab === "overview" && (
        <div className="sysmon-charts">
          <div className="sysmon-chart-card">
            <span className="sysmon-chart-title">CPU</span>
            <MiniChart data={chartData.map((d) => ({ t: d.t, value: d.cpu }))} dataKey="cpu" color="var(--color-accent)" range={timeRange} />
          </div>
          <div className="sysmon-chart-card">
            <span className="sysmon-chart-title">Memory</span>
            <MiniChart data={chartData.map((d) => ({ t: d.t, value: d.mem }))} dataKey="mem" color="var(--color-success)" range={timeRange} />
          </div>
          {hasGpu && (
            <div className="sysmon-chart-card">
              <span className="sysmon-chart-title">GPU</span>
              <MiniChart data={chartData.map((d) => ({ t: d.t, value: d.gpu }))} dataKey="gpu" color="var(--color-gpu)" range={timeRange} />
            </div>
          )}
        </div>
      )}

      {tab === "cores" && (
        <div className="sysmon-full-chart">
          <CoresChart history={activeHistory} range={timeRange} />
        </div>
      )}

      {tab === "processes" && (
        <div className="sysmon-full-chart">
          <ProcessCpuChart history={activeHistory} range={timeRange} />
        </div>
      )}
    </div>
  );
}
