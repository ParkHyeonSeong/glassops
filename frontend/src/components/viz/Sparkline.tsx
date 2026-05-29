import { useId } from "react";
import { ResponsiveContainer, AreaChart, Area, YAxis } from "recharts";

export interface SparklineProps {
  data: { t: number; v: number }[];
  color: string;
  height?: number;
  fixedDomain?: [number, number];
}

export default function Sparkline({ data, color, height = 36, fixedDomain }: SparklineProps) {
  const gradId = useId();
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.4} />
            <stop offset="100%" stopColor={color} stopOpacity={0.03} />
          </linearGradient>
        </defs>
        <YAxis domain={fixedDomain ?? ["auto", "auto"]} hide />
        <Area type="monotone" dataKey="v" stroke={color} strokeWidth={1.4}
          fill={`url(#${gradId})`} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
