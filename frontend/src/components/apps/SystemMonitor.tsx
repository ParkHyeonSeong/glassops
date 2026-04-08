import { useId, useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import { useMetricsStore } from "../../stores/metricsStore";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function Gauge({
  label,
  value,
  color,
  detail,
}: {
  label: string;
  value: number;
  color: string;
  detail?: string;
}) {
  const clampedValue = Math.min(100, Math.max(0, value));
  return (
    <div className="sysmon-gauge">
      <div className="sysmon-gauge-header">
        <span className="sysmon-gauge-label">{label}</span>
        <span className="sysmon-gauge-value" style={{ color }}>
          {clampedValue.toFixed(1)}%
        </span>
      </div>
      <div className="sysmon-gauge-track">
        <div
          className="sysmon-gauge-fill"
          style={{ width: `${clampedValue}%`, background: color }}
        />
      </div>
      {detail && <span className="sysmon-gauge-detail">{detail}</span>}
    </div>
  );
}

function MiniChart({
  data,
  dataKey,
  color,
}: {
  data: { t: string; value: number }[];
  dataKey: string;
  color: string;
}) {
  const gradId = useId();
  return (
    <ResponsiveContainer width="100%" height={100}>
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
        <defs>
          <linearGradient id={`${gradId}-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <XAxis dataKey="t" hide />
        <YAxis domain={[0, 100]} hide />
        <Tooltip
          contentStyle={{
            background: "rgba(20,20,40,0.9)",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 8,
            fontSize: 12,
            color: "#e0e0e0",
          }}
          formatter={(v) => [`${Number(v).toFixed(1)}%`]}
          labelFormatter={() => ""}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke={color}
          strokeWidth={1.5}
          fill={`url(#${gradId}-${dataKey})`}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export default function SystemMonitor() {
  const current = useMetricsStore((s) => s.current);
  const history = useMetricsStore((s) => s.history);
  const connected = useMetricsStore((s) => s.connected);

  const chartData = useMemo(() => {
    return history.map((m, i) => ({
      t: String(i),
      cpu: m.cpu.percent_total,
      mem: m.memory.percent,
      disk: m.disk.percent,
      gpu: m.gpu?.[0]?.gpu_util ?? 0,
    }));
  }, [history]);

  if (!connected || !current) {
    return (
      <div className="sysmon-empty">
        <p className="sysmon-empty-title">
          {connected ? "Waiting for data..." : "Connecting to server..."}
        </p>
        <p className="sysmon-empty-sub">
          Ensure the GlassOps Agent is running on your server.
        </p>
      </div>
    );
  }

  const { cpu, memory, disk, gpu } = current;
  const hasGpu = gpu && gpu.length > 0;

  return (
    <div className="sysmon">
      {/* Gauges */}
      <div className="sysmon-gauges">
        <Gauge
          label="CPU"
          value={cpu.percent_total}
          color="var(--color-accent)"
          detail={`${cpu.count_physical}C/${cpu.count_logical}T · ${cpu.freq_current.toFixed(0)} MHz`}
        />
        <Gauge
          label="Memory"
          value={memory.percent}
          color="var(--color-success)"
          detail={`${formatBytes(memory.used)} / ${formatBytes(memory.total)}`}
        />
        <Gauge
          label="Disk"
          value={disk.percent}
          color="var(--color-warning)"
          detail={`${formatBytes(disk.used)} / ${formatBytes(disk.total)}`}
        />
        {hasGpu && (
          <Gauge
            label={`GPU ${gpu[0].name}`}
            value={gpu[0].gpu_util}
            color="var(--color-gpu)"
            detail={`${gpu[0].temperature}°C · ${gpu[0].power_watts.toFixed(0)}W · VRAM ${formatBytes(gpu[0].mem_used)}/${formatBytes(gpu[0].mem_total)}`}
          />
        )}
      </div>

      {/* Charts */}
      <div className="sysmon-charts">
        <div className="sysmon-chart-card">
          <span className="sysmon-chart-title">CPU</span>
          <MiniChart
            data={chartData.map((d) => ({ t: d.t, value: d.cpu }))}
            dataKey="cpu"
            color="var(--color-accent)"
          />
        </div>
        <div className="sysmon-chart-card">
          <span className="sysmon-chart-title">Memory</span>
          <MiniChart
            data={chartData.map((d) => ({ t: d.t, value: d.mem }))}
            dataKey="mem"
            color="var(--color-success)"
          />
        </div>
        {hasGpu && (
          <div className="sysmon-chart-card">
            <span className="sysmon-chart-title">GPU</span>
            <MiniChart
              data={chartData.map((d) => ({ t: d.t, value: d.gpu }))}
              dataKey="gpu"
              color="var(--color-gpu)"
            />
          </div>
        )}
      </div>
    </div>
  );
}
