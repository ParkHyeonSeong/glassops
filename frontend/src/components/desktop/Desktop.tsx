import { useEffect } from "react";
import { useIsMobile } from "../../hooks/useIsMobile";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useKeyboardShortcuts } from "../../hooks/useKeyboardShortcuts";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAlertStore } from "../../stores/alertStore";
import { useSettingsStore, WALLPAPERS } from "../../stores/settingsStore";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { deriveAlerts, toastForAlert } from "../../lib/alerts";
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
  const push = useAlertStore((s) => s.push);
  const wallpaperId = useSettingsStore((s) => s.wallpaper);
  const thresholds = useThresholdsStore((s) => s.thresholds);

  const wallpaperCss = WALLPAPERS.find((w) => w.id === wallpaperId)?.css ?? WALLPAPERS[0].css;

  // Toasts share deriveAlerts() and the thresholds store with the System Monitor
  // banner/feed, so a threshold edited in one place applies everywhere.
  useEffect(() => {
    if (!current) return;
    for (const alert of deriveAlerts(current, thresholds)) {
      const toast = toastForAlert(alert);
      push(toast.type, toast.message, toast.key);
    }
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
        cpuPercent={cpuPercent}
        memPercent={memPercent}
      />
      <WindowManager />
      <Dock />
      <ToastContainer />
    </div>
  );
}
