import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { AreaChart, Area, XAxis, YAxis, ResponsiveContainer, Tooltip } from "recharts";
import { useMetricsStore } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";
import { StatusBadge, ContainerActionButtons, ContainerRemovedBanner } from "./dockerShared";
import { useContainerAction, formatBytes, type ContainerWindowProps } from "./dockerSharedUtils";
import { serverNowSeconds } from "../../utils/serverClock";
import { useServerNow } from "../../hooks/useServerNow";
import {
  boundContainerSamples,
  collapseByEffectiveTime,
  constrainContainerSamples,
  containerMetricsKey,
  effectiveSampleTime,
  mergeSamplesByTimestamp,
  RANGE_WINDOW_SEC,
  type ContainerSample,
  type TimeRange,
} from "./containerMetricsModel";

type ChartPoint = { t: number; value: number; bytes?: number };

interface ContainerSeriesActivation {
  key: string;
}

interface ContainerSeriesState {
  activation: ContainerSeriesActivation;
  samples: ContainerSample[];
  loading: boolean;
  error: string | null;
}

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
  const serverNow = useServerNow();
  const requestKey = containerMetricsKey(agentId, containerName, range);
  const activation = useMemo<ContainerSeriesActivation>(() => ({ key: requestKey }), [requestKey]);
  const [series, setSeries] = useState<ContainerSeriesState>({
    activation,
    samples: [],
    loading: range !== "live",
    error: null,
  });
  const seriesIsCurrent = series.activation === activation;
  const samples = useMemo(
    () => (seriesIsCurrent ? series.samples : []),
    [seriesIsCurrent, series.samples],
  );
  const loading = range !== "live" && (!seriesIsCurrent || series.loading);
  const error = seriesIsCurrent ? series.error : null;
  const action = useContainerAction(agentId, containerId);

  // History fetch on range change (skipped for "live" — it builds from pushes only).
  // Pushes that land during the fetch are preserved by merging on resolve.
  useEffect(() => {
    if (range === "live" || !agentId || !containerName) return;
    let cancelled = false;
    fetchWithAuth(
      `/api/metrics/${agentId}/containers/${encodeURIComponent(containerName)}/range?duration=${range}`,
    )
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        if (cancelled) return;
        const fetched: ContainerSample[] = (payload.metrics || []).map(
          (point: ContainerSample & { mem_limit?: number }) => ({
            t: point.t,
            cpu: point.cpu ?? 0,
            mem: point.mem ?? 0,
            mem_limit: point.mem_limit ?? 0,
            vram: point.vram ?? 0,
            gpu_util: point.gpu_util ?? 0,
            gpu_present: point.gpu_present ?? false,
          }),
        );
        setSeries((previous) => {
          const currentSamples = previous.activation === activation ? previous.samples : [];
          return {
            activation,
            samples: boundContainerSamples(
              mergeSamplesByTimestamp(fetched, currentSamples),
              range,
            ),
            loading: false,
            error: null,
          };
        });
      })
      .catch(() => {
        if (!cancelled) {
          setSeries((previous) => ({
            activation,
            samples: previous.activation === activation ? previous.samples : [],
            loading: false,
            error: "Failed to load history",
          }));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [activation, agentId, containerName, range]);

  // Live tail — applies to every range. Manual identity check on the per-agent
  // snapshot so the listener body only runs when this window's data changes,
  // not on unrelated store mutations (selectAgent / setConnected / loadHistory).
  const prevSnapRef = useRef<unknown>(null);
  useEffect(() => {
    if (!containerName) return;
    prevSnapRef.current = null;
    const unsubscribe = useMetricsStore.subscribe((state) => {
      const snap = state.agents[agentId]?.current ?? null;
      if (snap === prevSnapRef.current) return;
      prevSnapRef.current = snap;
      if (!snap) return;
      const c = (snap.containers ?? []).find((x) => x.name === containerName);
      if (!c) return;
      const t = snap.timestamp ?? serverNowSeconds();
      setSeries((previous) => {
        const previousIsCurrent = previous.activation === activation;
        const previousSamples = previousIsCurrent ? previous.samples : [];
        const last = previousSamples[previousSamples.length - 1];
        if (last && last.t === t) return previous;

        const sample: ContainerSample = {
          t,
          cpu: c.cpu_percent,
          mem: c.mem_usage,
          mem_limit: c.mem_limit,
          vram: c.gpu?.vram_bytes ?? 0,
          gpu_util: c.gpu?.gpu_util ?? 0,
          gpu_present: Boolean(c.gpu),
        };

        return {
          activation,
          samples: boundContainerSamples([...previousSamples, sample], range),
          loading: previousIsCurrent ? previous.loading : range !== "live",
          error: null,
        };
      });
    });
    return unsubscribe;
  }, [activation, range, containerName, agentId]);

  const data = useMemo(
    () => constrainContainerSamples(samples, range, serverNow),
    [samples, range, serverNow],
  );

  const memLimitBytes = data[data.length - 1]?.mem_limit ?? container?.mem_limit ?? 0;
  const chartData = useMemo(
    () => (range === "live"
      ? [...data].sort((left, right) => left.t - right.t)
      : collapseByEffectiveTime(data, serverNow)),
    [data, range, serverNow],
  );
  const chartTime = useCallback(
    (t: number): number =>
      range === "live" ? t : effectiveSampleTime(t, serverNow),
    [range, serverNow],
  );
  const memPctData = useMemo<ChartPoint[]>(
    () => chartData.map((d) => ({
      t: chartTime(d.t),
      value: d.mem_limit > 0 ? (d.mem / d.mem_limit) * 100 : 0,
      bytes: d.mem,
    })),
    [chartData, chartTime],
  );
  const cpuData = useMemo<ChartPoint[]>(
    () => chartData.map((d) => ({ t: chartTime(d.t), value: d.cpu })),
    [chartData, chartTime],
  );
  const vramData = useMemo<ChartPoint[]>(
    () => chartData.map((d) => ({
      t: chartTime(d.t),
      value: d.vram,
      bytes: d.vram,
    })),
    [chartData, chartTime],
  );
  const gpuUtilData = useMemo<ChartPoint[]>(
    () => chartData.map((d) => ({ t: chartTime(d.t), value: d.gpu_util })),
    [chartData, chartTime],
  );
  // Show GPU charts when reservation/usage was ever observed in the window or on
  // the live container — covers idle reservations and post-allocation periods.
  const hasGpu = data.some((d) => d.gpu_present) || !!container?.gpu;

  const avgCpu = data.length ? data.reduce((s, d) => s + d.cpu, 0) / data.length : 0;
  const peakCpu = data.length ? Math.max(...data.map((d) => d.cpu)) : 0;
  const avgMem = data.length ? data.reduce((s, d) => s + d.mem, 0) / data.length : 0;
  const peakMem = data.length ? Math.max(...data.map((d) => d.mem)) : 0;
  const avgVram = data.length ? data.reduce((s, d) => s + d.vram, 0) / data.length : 0;
  const peakVram = data.length ? Math.max(...data.map((d) => d.vram)) : 0;
  const avgGpuUtil = data.length ? data.reduce((s, d) => s + d.gpu_util, 0) / data.length : 0;
  const peakGpuUtil = data.length ? Math.max(...data.map((d) => d.gpu_util)) : 0;
  const memYMax = memLimitBytes > 0
    ? 100
    : Math.max(10, memPctData.reduce((m, d) => Math.max(m, d.value), 0) * 1.3);
  const vramYMax = Math.max(1, peakVram * 1.3);

  const xDomain: [number, number] | null =
    range === "live" ? null : [serverNow - RANGE_WINDOW_SEC[range], serverNow];

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
            valueFormatter={(v) => `${v.toFixed(1)}%`} yMax={Math.max(10, peakCpu * 1.3)}
            xDomain={xDomain} />
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
            xDomain={xDomain}
          />
        </div>

        {hasGpu && (
          <>
            <div className="sysmon-chart-card">
              <div className="docker-metrics-chart-header">
                <span className="sysmon-chart-title">GPU Util</span>
                <span className="docker-metrics-stats">
                  avg {avgGpuUtil.toFixed(1)}% · peak {peakGpuUtil.toFixed(1)}%
                </span>
              </div>
              <ContainerChart data={gpuUtilData} color="var(--color-gpu-util, #ff6ad5)" range={range}
                valueFormatter={(v) => `${v.toFixed(1)}%`} yMax={Math.max(10, peakGpuUtil * 1.3)}
                xDomain={xDomain} />
            </div>

            <div className="sysmon-chart-card">
              <div className="docker-metrics-chart-header">
                <span className="sysmon-chart-title">GPU VRAM</span>
                <span className="docker-metrics-stats">
                  avg {formatBytes(avgVram)} · peak {formatBytes(peakVram)}
                </span>
              </div>
              <ContainerChart data={vramData} color="var(--color-gpu, #b388ff)" range={range}
                valueFormatter={(v) => formatBytes(v)} yMax={vramYMax}
                xDomain={xDomain} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ContainerChart({
  data, color, range, valueFormatter, yMax, xDomain,
}: {
  data: ChartPoint[];
  color: string;
  range: TimeRange;
  valueFormatter: (v: number, payload?: ChartPoint) => string;
  yMax: number;
  xDomain: [number, number] | null;
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
          type="number"
          domain={xDomain ?? ["dataMin", "dataMax"]}
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
