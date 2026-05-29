import { useId } from "react";
import {
  ResponsiveContainer, AreaChart, LineChart, Area, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, Legend,
} from "recharts";
import { TOOLTIP_STYLE } from "./tokens";
import { type TimeRange, formatTime } from "./format";

export interface MetricSeries {
  key: string;          // key in each data row
  label: string;
  color: string;
  currentValue?: number; // shown in the legend
}

export interface ThresholdLine {
  value: number;
  label?: string;
  severity: "warn" | "crit";
}

export interface MetricChartProps {
  data: Array<Record<string, number> & { t: number }>;
  series: MetricSeries[];
  range: TimeRange;
  yDomain?: [number, number] | "auto"; // default: [0,100] when yUnit === "%"
  yTicks?: number[];                    // default: [0,25,50,75,100] when yUnit === "%"
  yUnit?: string;                       // e.g. "%"
  thresholds?: ThresholdLine[];
  height?: number;
  showLegend?: boolean;
  fillMode?: "area" | "line";           // area for single/% trends, line for overlap
}

const LINE_COLOR = { warn: "#f7971e", crit: "#f85032" } as const;

export default function MetricChart({
  data, series, range,
  yDomain, yTicks, yUnit = "",
  thresholds = [], height = 240,
  showLegend = true, fillMode = "area",
}: MetricChartProps) {
  const gradId = useId();
  const domain = (yDomain && yDomain !== "auto"
    ? yDomain
    : yUnit === "%" ? [0, 100] : ["auto", "auto"]) as [number, number];
  const ticks = yTicks ?? (yUnit === "%" ? [0, 25, 50, 75, 100] : undefined);
  const ChartComp = fillMode === "area" ? AreaChart : LineChart;

  // Custom legend showing current value + unit per series.
  const legendContent = () => (
    <ul className="viz-legend">
      {series.map((s) => (
        <li key={s.key} className="viz-legend-item">
          <span className="viz-legend-dot" style={{ background: s.color }} />
          <span className="viz-legend-label">{s.label}</span>
          {s.currentValue != null && (
            <span className="viz-legend-value">{s.currentValue.toFixed(1)}{yUnit}</span>
          )}
        </li>
      ))}
    </ul>
  );

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ChartComp data={data} margin={{ top: 10, right: 14, bottom: 18, left: 4 }}>
        {fillMode === "area" && (
          <defs>
            {series.map((s) => (
              <linearGradient key={s.key} id={`${gradId}-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={0.25} />
                <stop offset="100%" stopColor={s.color} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
        )}
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" vertical={false} />
        <XAxis
          dataKey="t"
          tick={{ fontSize: 10, fill: "var(--text-secondary)" }}
          tickLine={false}
          axisLine={{ stroke: "rgba(255,255,255,0.1)" }}
          interval="preserveStartEnd"
          minTickGap={60}
          tickFormatter={(ts) => formatTime(Number(ts), range)}
        />
        <YAxis
          domain={domain}
          ticks={ticks}
          width={40}
          tick={{ fontSize: 10, fill: "var(--text-secondary)" }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => `${v}${yUnit}`}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter={(v: any, name: any) => [`${Number(v ?? 0).toFixed(1)}${yUnit}`, name as string]}
          labelFormatter={(ts) => formatTime(Number(ts), range)}
        />
        {thresholds.map((t, i) => (
          <ReferenceLine
            key={i}
            y={t.value}
            stroke={LINE_COLOR[t.severity]}
            strokeDasharray="4 4"
            label={{
              value: t.label ?? `${t.value}${yUnit}`,
              position: "insideTopRight",
              fill: LINE_COLOR[t.severity],
              fontSize: 10,
            }}
          />
        ))}
        {series.map((s) =>
          fillMode === "area" ? (
            <Area key={s.key} type="monotone" dataKey={s.key} stroke={s.color}
              strokeWidth={1.75} fill={`url(#${gradId}-${s.key})`} isAnimationActive={false} />
          ) : (
            <Line key={s.key} type="monotone" dataKey={s.key} stroke={s.color}
              strokeWidth={1.75} dot={false} isAnimationActive={false} />
          ),
        )}
        {showLegend && <Legend content={legendContent} />}
      </ChartComp>
    </ResponsiveContainer>
  );
}
