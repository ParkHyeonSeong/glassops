import { useState, useEffect, useCallback } from "react";
import { FileText, Activity } from "lucide-react";
import { useMetricsStore, type ContainerInfo } from "../../stores/metricsStore";
import { useWindowStore } from "../../stores/windowStore";
import { fetchWithAuth } from "../../utils/api";
import { StatusBadge, ContainerActionButtons } from "./dockerShared";
import { formatBytes, type ContainerAction } from "./dockerSharedUtils";
import ContainerDetailDrawer from "./ContainerDetailDrawer";

type DockerTab = "containers" | "images" | "volumes" | "networks";

export default function DockerManager() {
  const [tab, setTab] = useState<DockerTab>("containers");

  return (
    <div className="docker-manager">
      <div className="docker-tabs">
        {(["containers", "images", "volumes", "networks"] as DockerTab[]).map((t) => (
          <button key={t} className={`sysmon-tab ${tab === t ? "sysmon-tab-active" : ""}`}
            onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      {tab === "containers" && <ContainersTab />}
      {tab === "images" && <ImagesTab />}
      {tab === "volumes" && <VolumesTab />}
      {tab === "networks" && <NetworksTab />}
    </div>
  );
}

/* ── Containers ── */
function ContainersTab() {
  const current = useMetricsStore((s) => s.current);
  const agentId = useMetricsStore((s) => s.agentId);
  const openWindow = useWindowStore((s) => s.openWindow);
  const containers: ContainerInfo[] = current?.containers ?? [];

  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = containers.find((c) => c.id === selectedId) || null;
  // Drop the drawer if the selected container disappears from the live list.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (selectedId && !containers.some((c) => c.id === selectedId)) setSelectedId(null);
  }, [containers, selectedId]);

  const doAction = useCallback(async (containerId: string, action: ContainerAction) => {
    setActionLoading(containerId);
    setActionError(null);
    try {
      const res = await fetchWithAuth(`/api/docker/containers/${containerId}/action`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setActionError(data.detail || `${action} failed`);
      }
    } catch {
      setActionError("Failed to connect to backend");
    }
    setActionLoading(null);
  }, []);

  const openLogsWindow = useCallback((containerName: string) => {
    if (!agentId) return;
    openWindow("container-logs", {
      params: { agentId, containerName },
      title: `Logs: ${containerName}`,
    });
  }, [agentId, openWindow]);

  const openMetricsWindow = useCallback((containerName: string) => {
    if (!agentId) return;
    openWindow("container-metrics", {
      params: { agentId, containerName },
      title: `Metrics: ${containerName}`,
    });
  }, [agentId, openWindow]);

  if (containers.length === 0) {
    return (
      <div className="docker-empty">
        <p className="docker-empty-title">No containers found</p>
        <p className="docker-empty-sub">Ensure Docker is running and GLASSOPS_ENABLE_DOCKER=true</p>
      </div>
    );
  }

  return (
    <div className="docker-containers-wrap">
      <div className="docker-containers-main">
        {actionError && <div className="docker-error" onClick={() => setActionError(null)}>{actionError}</div>}
        <table className="docker-table">
          <thead>
            <tr><th>Name</th><th>Image</th><th>Status</th><th>CPU</th><th>Memory</th><th>Ports</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {containers.map((c) => (
              <tr key={c.id}
                className={`${c.status === "running" ? "" : "docker-row-stopped"} ${selectedId === c.id ? "docker-row-selected" : ""} docker-row-clickable`}
                onClick={() => setSelectedId(c.id)}>
                <td className="docker-cell-name">{c.name}</td>
                <td className="docker-cell-image">{c.image}</td>
                <td><StatusBadge status={c.status} /></td>
                <td className="docker-cell-num">{c.cpu_percent.toFixed(1)}%</td>
                <td className="docker-cell-num">{c.mem_usage > 0 ? `${formatBytes(c.mem_usage)} / ${formatBytes(c.mem_limit)}` : "—"}</td>
                <td className="docker-cell-ports">{c.ports.length > 0 ? c.ports.join(", ") : "—"}</td>
                <td className="docker-cell-actions" onClick={(e) => e.stopPropagation()}>
                  <ContainerActionButtons
                    status={c.status}
                    removed={false}
                    loading={actionLoading === c.id}
                    onAction={(action) => doAction(c.id, action)}
                  />
                  <button className="docker-action-btn" onClick={() => openMetricsWindow(c.name)} title="Metrics history (new window)"><Activity size={13} /></button>
                  <button className="docker-action-btn" onClick={() => openLogsWindow(c.name)} title="Logs (new window)"><FileText size={13} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && (
        <ContainerDetailDrawer
          containerId={selected.id}
          status={selected.status}
          actionLoading={actionLoading === selected.id}
          onAction={doAction}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  );
}

/* ── Images ── */
function ImagesTab() {
  const [images, setImages] = useState<any[]>([]);
  useEffect(() => {
    fetchWithAuth("/api/docker/images").then((r) => r.json()).then((d) => setImages(d.images || [])).catch(() => {});
  }, []);
  return (
    <table className="docker-table">
      <thead><tr><th>ID</th><th>Tags</th><th>Size</th></tr></thead>
      <tbody>
        {images.map((img) => (
          <tr key={img.id}>
            <td className="docker-cell-image">{img.id}</td>
            <td>{img.tags?.join(", ") || "—"}</td>
            <td className="docker-cell-num">{formatBytes(img.size)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ── Volumes ── */
function VolumesTab() {
  const [volumes, setVolumes] = useState<any[]>([]);
  useEffect(() => {
    fetchWithAuth("/api/docker/volumes").then((r) => r.json()).then((d) => setVolumes(d.volumes || [])).catch(() => {});
  }, []);
  return (
    <table className="docker-table">
      <thead><tr><th>Name</th><th>Driver</th><th>Mountpoint</th></tr></thead>
      <tbody>
        {volumes.map((v) => (
          <tr key={v.name}>
            <td className="docker-cell-name">{v.name}</td>
            <td>{v.driver}</td>
            <td className="docker-cell-image">{v.mountpoint}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ── Networks ── */
function NetworksTab() {
  const [networks, setNetworks] = useState<any[]>([]);
  useEffect(() => {
    fetchWithAuth("/api/docker/networks").then((r) => r.json()).then((d) => setNetworks(d.networks || [])).catch(() => {});
  }, []);
  return (
    <table className="docker-table">
      <thead><tr><th>Name</th><th>Driver</th><th>Scope</th><th>ID</th></tr></thead>
      <tbody>
        {networks.map((n) => (
          <tr key={n.id}>
            <td className="docker-cell-name">{n.name}</td>
            <td>{n.driver}</td>
            <td>{n.scope}</td>
            <td className="docker-cell-image">{n.id}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
