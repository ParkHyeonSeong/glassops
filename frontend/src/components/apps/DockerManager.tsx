import { useState, useEffect, useCallback } from "react";
import { Play, Square, RotateCw, FileText, ChevronLeft } from "lucide-react";
import { useMetricsStore, type ContainerInfo } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function StatusBadge({ status }: { status: string }) {
  const isRunning = status === "running";
  const color = isRunning ? "var(--color-success)" : "var(--color-danger)";
  return (
    <span className="docker-status" style={{ color }}>
      <span className="docker-status-dot" style={{ background: color }} />
      {status}
    </span>
  );
}

export default function DockerManager() {
  const current = useMetricsStore((s) => s.current);
  const containers: ContainerInfo[] = current?.containers ?? [];

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [view, setView] = useState<"list" | "logs">("list");

  // Reset view if container disappears
  useEffect(() => {
    if (selectedId && !containers.find((c) => c.id === selectedId)) {
      setSelectedId(null);
      setView("list");
    }
  }, [containers, selectedId]);

  const doAction = useCallback(async (containerId: string, action: string) => {
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

  const showLogs = useCallback(async (containerId: string) => {
    setSelectedId(containerId);
    setView("logs");
    setLogsLoading(true);
    setLogs(null);
    try {
      const res = await fetchWithAuth(`/api/docker/containers/${containerId}/logs?tail=300`);
      if (res.ok) {
        const data = await res.json();
        setLogs(data.logs || "No logs available.");
      } else {
        setLogs("Failed to load logs.");
      }
    } catch {
      setLogs("Failed to connect to backend.");
    }
    setLogsLoading(false);
  }, []);

  if (containers.length === 0) {
    return (
      <div className="docker-empty">
        <p className="docker-empty-title">No containers found</p>
        <p className="docker-empty-sub">
          Ensure Docker is running and the Agent has GLASSOPS_ENABLE_DOCKER=true
        </p>
      </div>
    );
  }

  if (view === "logs" && selectedId) {
    const container = containers.find((c) => c.id === selectedId);
    return (
      <div className="docker-logs-view">
        <div className="docker-logs-header">
          <button className="docker-back-btn" onClick={() => setView("list")}>
            <ChevronLeft size={16} /> Back
          </button>
          <span className="docker-logs-title">{container?.name ?? selectedId}</span>
        </div>
        <pre className="docker-logs-content">
          {logsLoading ? "Loading logs..." : logs}
        </pre>
      </div>
    );
  }

  return (
    <div className="docker-manager">
      {actionError && (
        <div className="docker-error" onClick={() => setActionError(null)}>
          {actionError}
        </div>
      )}
      <table className="docker-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Image</th>
            <th>Status</th>
            <th>CPU</th>
            <th>Memory</th>
            <th>Ports</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {containers.map((c) => (
            <tr key={c.id} className={c.status === "running" ? "" : "docker-row-stopped"}>
              <td className="docker-cell-name">{c.name}</td>
              <td className="docker-cell-image">{c.image}</td>
              <td><StatusBadge status={c.status} /></td>
              <td className="docker-cell-num">{c.cpu_percent.toFixed(1)}%</td>
              <td className="docker-cell-num">
                {c.mem_usage > 0 ? `${formatBytes(c.mem_usage)} / ${formatBytes(c.mem_limit)}` : "—"}
              </td>
              <td className="docker-cell-ports">
                {c.ports.length > 0 ? c.ports.join(", ") : "—"}
              </td>
              <td className="docker-cell-actions">
                {c.status === "running" ? (
                  <>
                    <button
                      className="docker-action-btn docker-action-stop"
                      onClick={() => doAction(c.id, "stop")}
                      disabled={actionLoading === c.id}
                      title="Stop"
                    >
                      <Square size={13} />
                    </button>
                    <button
                      className="docker-action-btn docker-action-restart"
                      onClick={() => doAction(c.id, "restart")}
                      disabled={actionLoading === c.id}
                      title="Restart"
                    >
                      <RotateCw size={13} />
                    </button>
                  </>
                ) : (
                  <button
                    className="docker-action-btn docker-action-start"
                    onClick={() => doAction(c.id, "start")}
                    disabled={actionLoading === c.id}
                    title="Start"
                  >
                    <Play size={13} />
                  </button>
                )}
                <button
                  className="docker-action-btn"
                  onClick={() => showLogs(c.id)}
                  title="Logs"
                >
                  <FileText size={13} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
