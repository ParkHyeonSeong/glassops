import { type Severity } from "../../lib/thresholds";

export interface VitalCardProps {
  label: string;
  value: string;          // pre-formatted, e.g. "61"
  unit?: string;          // e.g. "%"
  sub?: string;           // e.g. "19.5 / 32 GB"
  percent?: number;       // 0-100 gauge fill
  severity: Severity;
  thresholdPercent?: number; // marker position on the gauge
  accentColor: string;
  onClick?: () => void;
}

export default function VitalCard({
  label, value, unit, sub, percent, severity, thresholdPercent, accentColor, onClick,
}: VitalCardProps) {
  const Tag: "button" | "div" = onClick ? "button" : "div";
  return (
    <Tag className={`viz-vital viz-sev-${severity}`} onClick={onClick}
      style={{ ["--accent" as string]: accentColor } as React.CSSProperties}>
      <div className="viz-vital-top">
        <span className="viz-vital-label">{label}</span>
        {severity !== "ok" && (
          <span className={`viz-vital-badge viz-sev-${severity}`}>
            {severity === "crit" ? "CRITICAL" : "WARNING"}
          </span>
        )}
      </div>
      <div className="viz-vital-value">
        {value}{unit && <span className="viz-vital-unit">{unit}</span>}
      </div>
      {sub && <div className="viz-vital-sub">{sub}</div>}
      {percent != null && (
        <div className="viz-vital-bar">
          <i style={{ width: `${Math.min(100, Math.max(0, percent))}%`, background: accentColor }} />
          {thresholdPercent != null && (
            <span className="viz-vital-thr" style={{ left: `${thresholdPercent}%` }} />
          )}
        </div>
      )}
    </Tag>
  );
}
