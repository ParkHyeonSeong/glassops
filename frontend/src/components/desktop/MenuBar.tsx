import { useState, useEffect } from "react";
import { Wifi, WifiOff } from "lucide-react";
import type { ConnectionStatus } from "../../types";

interface MenuBarProps {
  serverName?: string;
  connectionStatus: ConnectionStatus;
  cpuPercent?: number;
  memPercent?: number;
}

export default function MenuBar({
  serverName = "No Server",
  connectionStatus,
  cpuPercent,
  memPercent,
}: MenuBarProps) {
  const [time, setTime] = useState(new Date());

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
        <span className="menubar-server">{serverName}</span>
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
          {time.toLocaleTimeString("ko-KR", {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </div>
    </div>
  );
}
