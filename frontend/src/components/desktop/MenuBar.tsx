import { useState, useEffect } from "react";
import { Wifi, WifiOff, ChevronDown } from "lucide-react";
import type { ConnectionStatus } from "../../types";
import { useMetricsStore } from "../../stores/metricsStore";

interface MenuBarProps {
  connectionStatus: ConnectionStatus;
  cpuPercent?: number;
  memPercent?: number;
}

export default function MenuBar({
  connectionStatus,
  cpuPercent,
  memPercent,
}: MenuBarProps) {
  const [time, setTime] = useState(new Date());
  const agentIds = useMetricsStore((s) => s.agentIds);
  const selectedAgentId = useMetricsStore((s) => s.agentId);
  const selectAgent = useMetricsStore((s) => s.selectAgent);

  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  const statusColor =
    connectionStatus === "connected"
      ? "var(--color-success)"
      : connectionStatus === "connecting"
        ? "var(--color-warning)"
        : "var(--color-danger)";

  const StatusIcon = connectionStatus === "disconnected" ? WifiOff : Wifi;

  return (
    <div className="menubar">
      <div className="menubar-left">
        <span className="menubar-logo">GlassOps</span>
      </div>

      <div className="menubar-center">
        <StatusIcon size={13} style={{ color: statusColor }} />
        {agentIds.length > 1 ? (
          <div className="menubar-agent-select">
            <select
              value={selectedAgentId ?? ""}
              onChange={(e) => selectAgent(e.target.value)}
              className="menubar-agent-dropdown"
            >
              {agentIds.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
            <ChevronDown size={11} className="menubar-agent-chevron" />
          </div>
        ) : (
          <span className="menubar-server">
            {selectedAgentId ?? "No Agent"}
          </span>
        )}
        <span
          className="menubar-status-dot"
          style={{ backgroundColor: statusColor }}
        />
      </div>

      <div className="menubar-right">
        {cpuPercent !== undefined && (
          <span className="menubar-metric">CPU {cpuPercent}%</span>
        )}
        {memPercent !== undefined && (
          <span className="menubar-metric">MEM {memPercent}%</span>
        )}
        <span className="menubar-time">
          {time.toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </div>
    </div>
  );
}
