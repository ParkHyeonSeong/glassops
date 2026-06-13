import { useWindowStore } from "../../stores/windowStore";
import { useAuthStore } from "../../stores/authStore";
import Window from "./Window";
import SystemMonitor from "../apps/SystemMonitor";
import GpuMonitor from "../apps/GpuMonitor";
import DockerManager from "../apps/DockerManager";
import NetworkAnalyzer from "../apps/NetworkAnalyzer";
import ProcessViewer from "../apps/ProcessViewer";
import LogViewer from "../apps/LogViewer";
import TerminalApp from "../apps/Terminal";
import SettingsApp from "../apps/Settings";
import UserManager from "../apps/UserManager";
import AppPlaceholder from "../apps/AppPlaceholder";
import ContainerLogsWindow from "../apps/ContainerLogsWindow";
import ContainerMetricsWindow from "../apps/ContainerMetricsWindow";

function AppContent({
  appId,
  title,
  params,
}: {
  appId: string;
  title: string;
  params?: Record<string, string>;
}) {
  const role = useAuthStore((s) => s.role);
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
    case "users":
      // Defense-in-depth: the dock already hides this for non-admins, but a forged
      // window state must not render the admin UI. The API enforces authz regardless.
      if (role !== "admin") return <AppPlaceholder appId={appId} title={title} />;
      return <UserManager />;
    case "container-logs":
      if (!params?.containerName || !params?.agentId) return null;
      return <ContainerLogsWindow agentId={params.agentId} containerName={params.containerName} />;
    case "container-metrics":
      if (!params?.containerName || !params?.agentId) return null;
      return <ContainerMetricsWindow agentId={params.agentId} containerName={params.containerName} />;
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
          <AppContent appId={win.appId} title={win.title} params={win.params} />
        </Window>
      ))}
    </div>
  );
}
