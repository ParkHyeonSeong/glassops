import { useIsMobile } from "../../hooks/useIsMobile";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useKeyboardShortcuts } from "../../hooks/useKeyboardShortcuts";
import { useMetricsStore } from "../../stores/metricsStore";
import type { ConnectionStatus } from "../../types";
import MenuBar from "./MenuBar";
import Dock from "./Dock";
import WindowManager from "./WindowManager";
import MobileDesktop from "./MobileDesktop";

export default function Desktop() {
  const isMobile = useIsMobile();
  useWebSocket();
  useKeyboardShortcuts();

  const wsConnected = useMetricsStore((s) => s.connected);
  const current = useMetricsStore((s) => s.current);
  const agentId = useMetricsStore((s) => s.agentId);

  const connectionStatus: ConnectionStatus = wsConnected
    ? "connected"
    : "connecting";

  const cpuPercent = current
    ? Math.round(current.cpu.percent_total)
    : undefined;
  const memPercent = current ? Math.round(current.memory.percent) : undefined;

  if (isMobile) {
    return <MobileDesktop connectionStatus={connectionStatus} />;
  }

  return (
    <div className="desktop">
      <MenuBar
        connectionStatus={connectionStatus}
        serverName={agentId ? `Agent: ${agentId}` : undefined}
        cpuPercent={cpuPercent}
        memPercent={memPercent}
      />
      <WindowManager />
      <Dock />
    </div>
  );
}
