import { useState } from "react";
import { Bell } from "lucide-react";
import { type Alert } from "../../lib/alerts";

export interface AlertFeedProps {
  alerts: Alert[];
  onMute?: (alert: Alert) => void;
}

export default function AlertFeed({ alerts, onMute }: AlertFeedProps) {
  const [open, setOpen] = useState(false);
  return (
    <div className="viz-feed">
      <button className="viz-feed-bell" onClick={() => setOpen((o) => !o)} title="Alerts">
        <Bell size={14} />
        {alerts.length > 0 && <span className="viz-feed-count">{alerts.length}</span>}
      </button>
      {open && (
        <div className="viz-feed-panel">
          {alerts.length === 0 ? (
            <p className="viz-feed-empty">No active alerts</p>
          ) : (
            alerts.map((a) => (
              <div key={a.id} className={`viz-feed-item viz-sev-${a.severity}`}>
                <span className="viz-feed-dot" />
                <span className="viz-feed-text">{a.message}</span>
                {onMute && <button className="viz-feed-mute" onClick={() => onMute(a)}>Mute</button>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
