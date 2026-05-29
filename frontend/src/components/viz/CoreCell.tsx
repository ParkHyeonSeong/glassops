import { useId } from "react";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis } from "recharts";
import { type Severity, type Threshold, severityFor } from "../../lib/thresholds";
import { SEVERITY_COLORS } from "./tokens";

export interface CoreCellProps {
  index: number;
  data: { t: number; v: number }[];
  current: number;
  freqMhz?: number;       // current.cpu.freq_current
  threshold: Threshold;
  color: string;          // base color when severity is "ok"
}

export default function CoreCell({ index, data, current, freqMhz, threshold, color }: CoreCellProps) {
  const gradId = useId();
  const sev: Severity = severityFor(current, threshold);
  const stroke = sev === "ok" ? color : SEVERITY_COLORS[sev];
  return (
    <div className={`viz-core viz-sev-${sev}`}>
      <div className="viz-core-head">
        <span className="viz-core-label">Core {index}</span>
        <span className="viz-core-value" style={{ color: stroke }}>{current.toFixed(0)}%</span>
      </div>
      <div className="viz-core-chart">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.5} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0.04} />
              </linearGradient>
            </defs>
            <XAxis dataKey="t" hide />
            <YAxis domain={[0, 100]} hide />
            <Area type="monotone" dataKey="v" stroke={stroke} strokeWidth={1.2}
              fill={`url(#${gradId})`} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {freqMhz != null && <div className="viz-core-freq">{(freqMhz / 1000).toFixed(1)} GHz</div>}
    </div>
  );
}
