import { useWindowStore } from "../../stores/windowStore";
import Window from "./Window";
import SystemMonitor from "../apps/SystemMonitor";
import GpuMonitor from "../apps/GpuMonitor";
import DockerManager from "../apps/DockerManager";
import NetworkAnalyzer from "../apps/NetworkAnalyzer";
import ProcessViewer from "../apps/ProcessViewer";
import LogViewer from "../apps/LogViewer";
import TerminalApp from "../apps/Terminal";
import SettingsApp from "../apps/Settings";
import AppPlaceholder from "../apps/AppPlaceholder";

function AppContent({ appId, title }: { appId: string; title: string }) {
  switch (appId) {
    case "system-monitor":
      return <SystemMonitor />;
    case "gpu-monitor":
      return <GpuMonitor />;
    case "docker":
      return <DockerManager />;
    case "network":
      return <NetworkAnalyzer />;
    case "process":
      return <ProcessViewer />;
    case "logs":
      return <LogViewer />;
    case "terminal":
      return <TerminalApp />;
    case "settings":
      return <SettingsApp />;
    default:
      return <AppPlaceholder appId={appId} title={title} />;
  }
}

export default function WindowManager() {
  const windows = useWindowStore((s) => s.windows);

  return (
    <div className="window-manager">
      {windows.map((win) => (
        <Window key={win.id} window={win}>
          <AppContent appId={win.appId} title={win.title} />
        </Window>
      ))}
    </div>
  );
}
