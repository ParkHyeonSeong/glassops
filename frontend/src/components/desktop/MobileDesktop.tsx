import { useState } from "react";
import {
  BarChart3,
  Box,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
  Wifi,
  WifiOff,
} from "lucide-react";
import { APP_DEFINITIONS } from "../../stores/windowStore";
import type { ConnectionStatus } from "../../types";
import { useServerTime } from "../../hooks/useServerTime";
import AppPlaceholder from "../apps/AppPlaceholder";
import SystemMonitor from "../apps/SystemMonitor";
import GpuMonitor from "../apps/GpuMonitor";
import DockerManager from "../apps/DockerManager";
import NetworkAnalyzer from "../apps/NetworkAnalyzer";
import ProcessViewer from "../apps/ProcessViewer";
import LogViewer from "../apps/LogViewer";
import TerminalApp from "../apps/Terminal";
import SettingsApp from "../apps/Settings";

const ICON_MAP: Record<string, React.ComponentType<{ size?: number }>> = {
  BarChart3,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
};

const FallbackIcon = Box;

interface MobileDesktopProps {
  connectionStatus: ConnectionStatus;
}

export default function MobileDesktop({
  connectionStatus,
}: MobileDesktopProps) {
  const [activeAppId, setActiveAppId] = useState<string | null>(null);
  const time = useServerTime();

  const statusColor =
    connectionStatus === "connected"
      ? "var(--color-success)"
      : connectionStatus === "connecting"
        ? "var(--color-warning)"
        : "var(--color-danger)";

  const StatusIcon = connectionStatus === "disconnected" ? WifiOff : Wifi;

  const activeApp = APP_DEFINITIONS.find((a) => a.id === activeAppId);

  return (
    <div className="mobile-desktop">
      {/* Mobile Menu Bar */}
      <div className="mobile-menubar">
        <div className="menubar-left">
          {activeApp ? (
            <button
              className="mobile-back-btn"
              onClick={() => setActiveAppId(null)}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          ) : (
            <span className="menubar-logo">GlassOps</span>
          )}
          {activeApp && (
            <span className="mobile-app-title">{activeApp.title}</span>
          )}
        </div>
        <div className="menubar-right">
          <StatusIcon size={13} style={{ color: statusColor }} />
          <span className="menubar-time">
            {time.toLocaleTimeString("ko-KR", {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </span>
        </div>
      </div>

      {/* Content */}
      {activeAppId && activeApp ? (
        <div className="mobile-app-content">
          {activeAppId === "system-monitor" ? (
            <SystemMonitor />
          ) : activeAppId === "gpu-monitor" ? (
            <GpuMonitor />
          ) : activeAppId === "docker" ? (
            <DockerManager />
          ) : activeAppId === "network" ? (
            <NetworkAnalyzer />
          ) : activeAppId === "process" ? (
            <ProcessViewer />
          ) : activeAppId === "logs" ? (
            <LogViewer />
          ) : activeAppId === "terminal" ? (
            <TerminalApp />
          ) : activeAppId === "settings" ? (
            <SettingsApp />
          ) : (
            <AppPlaceholder appId={activeApp.id} title={activeApp.title} />
          )}
        </div>
      ) : (
        <div className="mobile-app-grid">
          {APP_DEFINITIONS.map((app) => {
            const Icon = ICON_MAP[app.icon] ?? FallbackIcon;
            return (
              <button
                key={app.id}
                className="mobile-app-icon"
                onClick={() => setActiveAppId(app.id)}
              >
                <div className="mobile-app-icon-circle">
                  <Icon size={26} />
                </div>
                <span className="mobile-app-label">{app.title}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
