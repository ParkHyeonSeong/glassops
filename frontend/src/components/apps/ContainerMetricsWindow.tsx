import { useState, useEffect, useMemo, useId } from "react";
import { AreaChart, Area, XAxis, YAxis, ResponsiveContainer, Tooltip } from "recharts";
import { useMetricsStore } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";
import { StatusBadge, ContainerActionButtons, ContainerRemovedBanner } from "./dockerShared";
import { useContainerAction, formatBytes, type ContainerWindowProps } from "./dockerSharedUtils";

type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";
type ContainerSample = { t: number; cpu: number; mem: number; mem_limit: number; vram: number };
type ChartPoint = { t: number; value: number; bytes?: number };

const TOOLTIP_STYLE = {
  background: "rgba(20,20,40,0.9)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8,
  fontSize: 11,
  color: "#e0e0e0",
};

function formatChartTime(ts: number, range: TimeRange): string {
  const d = new Date(ts * 1000);
  if (range === "live" || range === "5m" || range === "1h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  if (range === "6h" || range === "24h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function ContainerMetricsWindow({ agentId, containerName }: ContainerWindowProps) {
  // Look up against THIS window's agent — see ContainerLogsWindow for rationale.
  const container = useMetricsStore(
    (s) => s.agents[agentId]?.current?.containers?.find((c) => c.name === containerName),
  );
  const containerId = container?.id ?? null;
  const removed = !container;

  const [range, setRange] = useState<TimeRange>("1h");
  const [history, setHistory] = useState<ContainerSample[]>([]);
  const [liveSamples, setLiveSamples] = useState<ContainerSample[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const action = useContainerAction(agentId, containerId);

  useEffect(() => {
    setLiveSamples([]);
  }, [containerName, range]);

  useEffect(() => {
    if (range !== "live" || !containerName) return;
    const unsubscribe = useMetricsStore.subscribe((state) => {
      // Read this window's agent specifically — `state.current` follows MenuBar
      // selection and would deliver wrong data after a host switch.
      const snap = state.agents[agentId]?.current;
      if (!snap) return;
      const c = (snap.containers ?? []).find((x) => x.name === containerName);
      if (!c) return;
      const t = snap.timestamp ?? Date.now() / 1000;
      setLiveSamples((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.t === t) return prev;
        const next = [
          ...prev,
          {
            t,
            cpu: c.cpu_percent,
            mem: c.mem_usage,
            mem_limit: c.mem_limit,
            vram: c.gpu?.vram_bytes ?? 0,
          },
        ];
        return next.length > 120 ? next.slice(-120) : next;
      });
    });
    return unsubscribe;
  }, [range, containerName, agentId]);

  useEffect(() => {
    if (range === "live") {
      setHistory([]);
      setError(null);
      return;
    }
    if (!agentId || !containerName) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchWithAuth(`/api/metrics/${agentId}/containers/${encodeURIComponent(containerName)}/range?duration=${range}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => { if (!cancelled) setHistory(d.metrics || []); })
      .catch(() => { if (!cancelled) { setHistory([]); setError("Failed to load history"); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [agentId, containerName, range]);

  const data: ContainerSample[] = range === "live" ? liveSamples : history;

  const memLimitBytes = data[data.length - 1]?.mem_limit ?? container?.mem_limit ?? 0;
  const memPctData = useMemo<ChartPoint[]>(
    () => data.map((d) => ({
      t: d.t,
      value: d.mem_limit > 0 ? (d.mem / d.mem_limit) * 100 : 0,
      bytes: d.mem,
    })),
    [data],
  );
  const cpuData = useMemo<ChartPoint[]>(
    () => data.map((d) => ({ t: d.t, value: d.cpu })),
    [data],
  );
  const vramData = useMemo<ChartPoint[]>(
    () => data.map((d) => ({ t: d.t, value: d.vram, bytes: d.vram })),
    [data],
  );
  const hasVram = data.some((d) => d.vram > 0) || (container?.gpu?.vram_bytes ?? 0) > 0;

  const avgCpu = data.length ? data.reduce((s, d) => s + d.cpu, 0) / data.length : 0;
  const peakCpu = data.length ? Math.max(...data.map((d) => d.cpu)) : 0;
  const avgMem = data.length ? data.reduce((s, d) => s + d.mem, 0) / data.length : 0;
  const peakMem = data.length ? Math.max(...data.map((d) => d.mem)) : 0;
  const avgVram = data.length ? data.reduce((s, d) => s + d.vram, 0) / data.length : 0;
  const peakVram = data.length ? Math.max(...data.map((d) => d.vram)) : 0;
  const memYMax = memLimitBytes > 0
    ? 100
    : Math.max(10, memPctData.reduce((m, d) => Math.max(m, d.value), 0) * 1.3);
  const vramYMax = Math.max(1, peakVram * 1.3);

  return (
    <div className="docker-metrics-view">
      <div className="docker-window-header">
        <span className="docker-logs-title">{containerName}</span>
        {container && <StatusBadge status={container.status} />}
        <div className="docker-window-actions">
          <ContainerActionButtons
            status={container?.status}
            removed={removed}
            loading={action.loading}
            onAction={action.run}
          />
        </div>
      </div>

      {removed && <ContainerRemovedBanner containerName={containerName} />}
      {action.error && (
        <div className="docker-error" onClick={action.clearError}>{action.error}</div>
      )}

      <div className="docker-metrics-toolbar">
        <div className="sysmon-tabs">
          {(["live", "5m", "1h", "6h", "24h", "7d"] as TimeRange[]).map((r) => (
            <button key={r} className={`sysmon-tab ${range === r ? "sysmon-tab-active" : ""}`}
              onClick={() => setRange(r)}>
              {r === "live" ? "Live" : r}
            </button>
          ))}
        </div>
        {loading && <span className="sysmon-loading">Loading...</span>}
        {error && <span className="docker-logs-range-error">{error}</span>}
        {!loading && !error && data.length === 0 && range !== "live" && (
          <span className="sysmon-loading">No data for this range yet.</span>
        )}
        {range === "live" && data.length === 0 && (
          <span className="sysmon-loading">Waiting for live samples…</span>
        )}
      </div>

      <div className="docker-metrics-charts">
        <div className="sysmon-chart-card">
          <div className="docker-metrics-chart-header">
            <span className="sysmon-chart-title">CPU</span>
            <span className="docker-metrics-stats">
              avg {avgCpu.toFixed(1)}% · peak {peakCpu.toFixed(1)}%
            </span>
          </div>
          <ContainerChart data={cpuData} color="var(--color-accent)" range={range}
            valueFormatter={(v) => `${v.toFixed(1)}%`} yMax={Math.max(10, peakCpu * 1.3)} />
        </div>

        <div className="sysmon-chart-card">
          <div className="docker-metrics-chart-header">
            <span className="sysmon-chart-title">Memory</span>
            <span className="docker-metrics-stats">
              avg {formatBytes(avgMem)} · peak {formatBytes(peakMem)}
              {memLimitBytes > 0 && ` · limit ${formatBytes(memLimitBytes)}`}
            </span>
          </div>
          <ContainerChart
            data={memPctData}
            color="var(--color-success)"
            range={range}
            valueFormatter={(v, p) => `${formatBytes(p?.bytes ?? 0)} (${v.toFixed(1)}%)`}
            yMax={memYMax}
          />
        </div>

        {hasVram && (
          <div className="sysmon-chart-card">
            <div className="docker-metrics-chart-header">
              <span className="sysmon-chart-title">GPU VRAM</span>
              <span className="docker-metrics-stats">
                avg {formatBytes(avgVram)} · peak {formatBytes(peakVram)}
              </span>
            </div>
            <ContainerChart data={vramData} color="var(--color-gpu, #b388ff)" range={range}
              valueFormatter={(v) => formatBytes(v)} yMax={vramYMax} />
          </div>
        )}
      </div>
    </div>
  );
}

function ContainerChart({
  data, color, range, valueFormatter, yMax,
}: {
  data: ChartPoint[];
  color: string;
  range: TimeRange;
  valueFormatter: (v: number, payload?: ChartPoint) => string;
  yMax: number;
}) {
  const gradId = useId();
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 18, left: 4 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="t"
          tick={{ fontSize: 9, fill: "var(--text-secondary)" }}
          tickLine={false}
          axisLine={false}
          interval="preserveStartEnd"
          minTickGap={60}
          tickFormatter={(ts) => formatChartTime(Number(ts), range)}
        />
        <YAxis domain={[0, Math.max(yMax, 1)]} hide />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v, _name, item) => [valueFormatter(Number(v), item?.payload as ChartPoint | undefined)]}
          labelFormatter={(ts) => formatChartTime(Number(ts), range)}
        />
        <Area type="monotone" dataKey="value" stroke={color} strokeWidth={1.5}
          fill={`url(#${gradId})`} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
