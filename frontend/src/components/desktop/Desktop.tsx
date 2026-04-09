import { useEffect } from "react";
import { useIsMobile } from "../../hooks/useIsMobile";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useKeyboardShortcuts } from "../../hooks/useKeyboardShortcuts";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAlertStore } from "../../stores/alertStore";
import { useSettingsStore, WALLPAPERS } from "../../stores/settingsStore";
import type { ConnectionStatus } from "../../types";
import MenuBar from "./MenuBar";
import Dock from "./Dock";
import WindowManager from "./WindowManager";
import MobileDesktop from "./MobileDesktop";
import ToastContainer from "../common/ToastContainer";

// Request notification permission on mount
function useNotificationPermission() {
  useEffect(() => {
    if ("Notification" in globalThis && Notification.permission === "default") {
      Notification.requestPermission();
    }
  }, []);
}

export default function Desktop() {
  const isMobile = useIsMobile();
  useWebSocket();
  useKeyboardShortcuts();
  useNotificationPermission();

  const wsConnected = useMetricsStore((s) => s.connected);
  const current = useMetricsStore((s) => s.current);
  const agentId = useMetricsStore((s) => s.agentId);
  const push = useAlertStore((s) => s.push);
  const wallpaperId = useSettingsStore((s) => s.wallpaper);
  const thresholds = useSettingsStore((s) => s.alertThresholds);

  const wallpaperCss = WALLPAPERS.find((w) => w.id === wallpaperId)?.css ?? WALLPAPERS[0].css;

  // Monitor thresholds
  useEffect(() => {
    if (!current) return;
    const { cpu, memory, disk } = current;
    if (cpu.percent_total > thresholds.cpuCrit) push("error", `CPU critical: ${cpu.percent_total.toFixed(1)}%`, "cpu-high");
    else if (cpu.percent_total > thresholds.cpuWarn) push("warning", `CPU high: ${cpu.percent_total.toFixed(1)}%`, "cpu-warn");
    if (memory.percent > thresholds.memCrit) push("error", `Memory critical: ${memory.percent.toFixed(1)}%`, "mem-high");
    else if (memory.percent > thresholds.memWarn) push("warning", `Memory high: ${memory.percent.toFixed(1)}%`, "mem-warn");
    if (disk.percent > thresholds.diskCrit) push("error", `Disk critical: ${disk.percent.toFixed(1)}%`, "disk-high");
  }, [current, push, thresholds]);

  const connectionStatus: ConnectionStatus = wsConnected ? "connected" : "connecting";
  const cpuPercent = current ? Math.round(current.cpu.percent_total) : undefined;
  const memPercent = current ? Math.round(current.memory.percent) : undefined;

  if (isMobile) {
    return (
      <>
        <MobileDesktop connectionStatus={connectionStatus} />
        <ToastContainer />
      </>
    );
  }

  return (
    <div className="desktop" style={{ background: wallpaperCss }}>
      <MenuBar
        connectionStatus={connectionStatus}
        serverName={agentId ? `Agent: ${agentId}` : undefined}
        cpuPercent={cpuPercent}
        memPercent={memPercent}
      />
      <WindowManager />
      <Dock />
      <ToastContainer />
    </div>
  );
}
