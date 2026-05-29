import { useEffect, useId, useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
} from "recharts";
import { useMetricsStore } from "../../stores/metricsStore";
import { useWindowStore } from "../../stores/windowStore";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { useActiveAlerts } from "../../hooks/useActiveAlerts";
import { fetchWithAuth } from "../../utils/api";
import { type TimeRange, formatBytes, formatRate } from "../../components/viz/format";
import { METRIC_COLORS, CORE_COLORS } from "../../components/viz/tokens";
import MetricChart from "../../components/viz/MetricChart";
import VitalCard from "../../components/viz/VitalCard";
import Sparkline from "../../components/viz/Sparkline";
import AlertBanner from "../../components/viz/AlertBanner";
import AlertFeed from "../../components/viz/AlertFeed";
import ThresholdSettings from "../../components/viz/ThresholdSettings";
import { severityFor } from "../../lib/thresholds";
import type { Alert } from "../../lib/alerts";

type Tab = "overview" | "cores";

function CoresChart({ history }: { history: any[] }) {
  // Prefer the authoritative count from the CPU metric (also the basis for the
  // tab label) so an in-flight gap snapshot can't make us under-count cores.
  const latest = history[history.length - 1]?.cpu;
  const latestPerCore: number[] = latest?.percent_per_core ?? [];
  const coreCount: number = latest?.count_logical
    ?? latestPerCore.length
    ?? history[0]?.cpu?.percent_per_core?.length
    ?? 0;

  // Build one timeseries per core in a single pass so the grid renders many
  // cards cheaply — each cell then just reads its slice.
  // TODO: virtualize for hosts with 128+ cores (react-window) — current design
  // mounts one ResponsiveContainer per core which is fine up to ~64.
  const perCore: { t: number; v: number }[][] = useMemo(() => {
    const out: { t: number; v: number }[][] = Array.from({ length: coreCount }, () => []);
    for (const m of history as any[]) {
      const t = m.timestamp ?? 0;
      const cores = m.cpu?.percent_per_core ?? [];
      for (let i = 0; i < coreCount; i++) {
        out[i].push({ t, v: cores[i] ?? 0 });
      }
    }
    return out;
  }, [history, coreCount]);

  if (coreCount === 0) return <p className="sysmon-empty-sub">No core data</p>;

  return (
    <div className="sysmon-cores-grid">
      {Array.from({ length: coreCount }, (_, i) => (
        <CoreCell
          key={i}
          index={i}
          color={CORE_COLORS[i % CORE_COLORS.length]}
          data={perCore[i]}
          current={latestPerCore[i] ?? 0}
        />
      ))}
    </div>
  );
}

function CoreCell({
  index,
  color,
  data,
  current,
}: {
  index: number;
  color: string;
  data: { t: number; v: number }[];
  current: number;
}) {
  const gradId = useId();
  return (
    <div className="sysmon-core-cell">
      <div className="sysmon-core-header">
        <span className="sysmon-core-label">Core {index}</span>
        <span className="sysmon-core-value" style={{ color }}>{current.toFixed(0)}%</span>
      </div>
      <div className="sysmon-core-chart">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.5} />
                <stop offset="100%" stopColor={color} stopOpacity={0.04} />
              </linearGradient>
            </defs>
            <XAxis dataKey="t" hide />
            <YAxis domain={[0, 100]} hide />
            <Area type="monotone" dataKey="v" stroke={color} strokeWidth={1.2}
              fill={`url(#${gradId})`} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
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
  const openWindow = useWindowStore((s) => s.openWindow);
  const [tab, setTab] = useState<Tab>("overview");
  const [timeRange, setTimeRange] = useState<TimeRange>("live");

  const activeAlerts = useActiveAlerts();
  const thresholds = useThresholdsStore((s) => s.thresholds);
  const muteAlert = useThresholdsStore((s) => s.mute);
  const handleMute = (a: Alert) => muteAlert(a.id, Date.now() + 60 * 60 * 1000); // 1h

  const sevFor = (key: "cpu" | "mem" | "disk", value: number) =>
    severityFor(value, thresholds[key]);

  const { data: historicalData, loading: histLoading } = useHistoricalData(agentId, timeRange);
  const activeHistory = timeRange === "live" ? history : historicalData;

  const chartData = useMemo(() =>
    activeHistory.map((m: { timestamp?: number; cpu?: { percent_total?: number }; memory?: { percent?: number }; disk?: { percent?: number } }) => ({
      t: m.timestamp ?? 0,
      cpu: m.cpu?.percent_total ?? 0,
      mem: m.memory?.percent ?? 0,
      disk: m.disk?.percent ?? 0,
    })), [activeHistory]);

  if (!connected || !current) {
    return (
      <div className="sysmon-empty">
        <p className="sysmon-empty-title">{connected ? "Waiting for data..." : "Connecting to server..."}</p>
        <p className="sysmon-empty-sub">Ensure the GlassOps Agent is running on your server.</p>
      </div>
    );
  }

  const { cpu, memory, disk, network, gpu } = current;
  const hasGpu = !!gpu && gpu.length > 0;
  const diskThreshold = thresholds.disk;

  return (
    <div className="sysmon">
      {/* header: tabs + time range + alert bell */}
      <div className="sysmon-controls">
        <div className="sysmon-tabs">
          {(["overview", "cores"] as Tab[]).map((t) => (
            <button key={t} className={`sysmon-tab ${tab === t ? "sysmon-tab-active" : ""}`}
              onClick={() => setTab(t)}>
              {t === "overview" ? "Overview" : `Cores (${cpu.count_logical})`}
            </button>
          ))}
        </div>
        <div className="sysmon-header-right">
          <div className="sysmon-tabs">
            {(["live", "5m", "1h", "6h", "24h", "7d"] as TimeRange[]).map((r) => (
              <button key={r} className={`sysmon-tab ${timeRange === r ? "sysmon-tab-active" : ""}`}
                onClick={() => setTimeRange(r)}>
                {r === "live" ? "Live" : r}
              </button>
            ))}
          </div>
          <AlertFeed alerts={activeAlerts} onMute={handleMute} />
          <ThresholdSettings />
        </div>
      </div>

      <AlertBanner alerts={activeAlerts} onMute={handleMute} />

      {histLoading && <p className="sysmon-loading">Loading...</p>}

      {tab === "overview" && (
        <>
          <div className="sysmon-vitals">
            <VitalCard label="CPU" value={cpu.percent_total.toFixed(0)} unit="%"
              sub={`${cpu.count_physical}C / ${cpu.count_logical}T · ${(cpu.freq_current / 1000).toFixed(1)} GHz`}
              percent={cpu.percent_total} severity={sevFor("cpu", cpu.percent_total)}
              thresholdPercent={thresholds.cpu.warn} accentColor={METRIC_COLORS.cpu} />
            <VitalCard label="Memory" value={memory.percent.toFixed(0)} unit="%"
              sub={`${formatBytes(memory.used)} / ${formatBytes(memory.total)}`}
              percent={memory.percent} severity={sevFor("mem", memory.percent)}
              thresholdPercent={thresholds.mem.warn} accentColor={METRIC_COLORS.mem} />
            <VitalCard label="Disk" value={disk.percent.toFixed(0)} unit="%"
              sub={`${formatBytes(disk.used)} / ${formatBytes(disk.total)}`}
              percent={disk.percent} severity={sevFor("disk", disk.percent)}
              thresholdPercent={thresholds.disk.warn} accentColor={METRIC_COLORS.disk} />
            <VitalCard label="Network"
              value={network ? formatRate(network.rates.recv_rate) : "—"}
              sub={network ? `↑ ${formatRate(network.rates.send_rate)}` : "no data"}
              severity="ok" accentColor={METRIC_COLORS.net} />
            {hasGpu && (
              <VitalCard label={`GPU ×${gpu!.length}`} value={gpu![0].gpu_util.toFixed(0)} unit="%"
                sub="Open GPU Monitor →" severity="ok" accentColor={METRIC_COLORS.gpu}
                onClick={() => openWindow("gpu-monitor")} />
            )}
          </div>

          <div className="sysmon-hero-chart">
            <div className="sysmon-card-head"><h3>Host Resource Trend</h3><span className="sysmon-card-unit">Y: 0–100%</span></div>
            <MetricChart
              data={chartData}
              range={timeRange}
              yUnit="%"
              height={260}
              thresholds={[{ value: diskThreshold.warn, severity: "warn", label: `Disk warn ${diskThreshold.warn}%` }]}
              series={[
                { key: "cpu", label: "CPU", color: METRIC_COLORS.cpu, currentValue: cpu.percent_total },
                { key: "mem", label: "Memory", color: METRIC_COLORS.mem, currentValue: memory.percent },
                { key: "disk", label: "Disk", color: METRIC_COLORS.disk, currentValue: disk.percent },
              ]}
            />
          </div>

          {network && (
            <div className="sysmon-secondary">
              <div className="sysmon-spark-card">
                <span className="sysmon-spark-label">Network ↓ {formatRate(network.rates.recv_rate)}</span>
                <Sparkline data={chartData.map((d) => ({ t: d.t, v: 0 }))} color={METRIC_COLORS.cpu} />
              </div>
              <div className="sysmon-spark-card">
                <span className="sysmon-spark-label">Network ↑ {formatRate(network.rates.send_rate)}</span>
                <Sparkline data={chartData.map((d) => ({ t: d.t, v: 0 }))} color={METRIC_COLORS.mem} />
              </div>
            </div>
          )}
        </>
      )}

      {tab === "cores" && (
        <div className="sysmon-full-chart">
          <CoresChart history={activeHistory} />
        </div>
      )}
    </div>
  );
}
