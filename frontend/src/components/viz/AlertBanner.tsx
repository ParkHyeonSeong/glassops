import { type Alert } from "../../lib/alerts";

export interface AlertBannerProps {
  alerts: Alert[];                 // already sorted by severity
  onMute?: (alert: Alert) => void;
}

export default function AlertBanner({ alerts, onMute }: AlertBannerProps) {
  if (alerts.length === 0) return null;
  const top = alerts[0];
  const lvl = top.severity === "crit" ? "CRITICAL" : "WARNING";
  return (
    <div className={`viz-banner viz-sev-${top.severity}`}>
      <div className="viz-banner-badge">
        <span className="viz-banner-lvl">{lvl}</span>
        <span className="viz-banner-sub">{alerts.length} alert{alerts.length > 1 ? "s" : ""}</span>
      </div>
      <div className="viz-banner-msg">
        <div className="viz-banner-h"><span className="viz-banner-pulse" />{top.message}</div>
        {alerts.length > 1 && <div className="viz-banner-d">+{alerts.length - 1} more active</div>}
      </div>
      <div className="viz-banner-acts">
        {onMute && <button onClick={() => onMute(top)}>Mute 1h</button>}
      </div>
    </div>
  );
}
