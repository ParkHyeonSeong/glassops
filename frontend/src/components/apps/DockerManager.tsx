import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { Play, Square, RotateCw, FileText, ChevronLeft, Search, ChevronUp, ChevronDown, X, Calendar } from "lucide-react";
import { useMetricsStore, type ContainerInfo } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";
import { useLogStream } from "../../hooks/useLogStream";

type DockerTab = "containers" | "images" | "volumes" | "networks";

const TAIL_INITIAL = 300;
const TAIL_MAX = 2000;

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
  const containers: ContainerInfo[] = current?.containers ?? [];

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [view, setView] = useState<"list" | "logs">("list");
  const [keyword, setKeyword] = useState("");
  const [currentMatchRaw, setCurrentMatchRaw] = useState(0);
  const [rangeFrom, setRangeFrom] = useState("");
  const [rangeTo, setRangeTo] = useState("");
  const [rangeActive, setRangeActive] = useState(false);
  const [rangeError, setRangeError] = useState<string | null>(null);
  const logsRef = useRef<HTMLPreElement | null>(null);
  const preserveScrollRef = useRef(false);
  const prevScrollHeightRef = useRef(0);
  const prevScrollTopRef = useRef(0);
  const logsRequestIdRef = useRef(0);
  const matchElsRef = useRef<Array<HTMLElement | null>>([]);
  const scrollToMatchRef = useRef(false);
  // Live-tail follow: when user is near the bottom we auto-scroll new chunks into view.
  const followRef = useRef(true);

  const handleLogChunk = useCallback((chunk: string) => {
    setLogs((prev) => (prev ?? "") + chunk);
  }, []);

  // Streaming is active in the logs view as long as no historical date range is applied.
  const streamEnabled = view === "logs" && !!selectedId && !rangeActive;
  const stream = useLogStream({
    containerId: streamEnabled ? selectedId : null,
    agentId: agentId ?? null,
    tail: TAIL_INITIAL,
    enabled: streamEnabled,
    onLine: handleLogChunk,
  });

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

  const fetchLogs = useCallback(async (
    containerId: string,
    tail: number,
    preserveScroll: boolean,
    since?: string,
    until?: string,
  ) => {
    const reqId = ++logsRequestIdRef.current;
    preserveScrollRef.current = preserveScroll;
    if (preserveScroll && logsRef.current) {
      prevScrollHeightRef.current = logsRef.current.scrollHeight;
      prevScrollTopRef.current = logsRef.current.scrollTop;
    }
    setLogsLoading(true);
    try {
      const params = new URLSearchParams({ tail: String(tail) });
      if (since) params.set("since", since);
      if (until) params.set("until", until);
      const res = await fetchWithAuth(`/api/docker/containers/${containerId}/logs?${params.toString()}`);
      if (reqId !== logsRequestIdRef.current) return;
      if (res.ok) {
        const data = await res.json();
        setLogs(data.logs || "No logs available.");
      } else {
        const data = await res.json().catch(() => ({}));
        setLogs(`Failed to load logs: ${data.detail || res.status}`);
      }
    } catch {
      if (reqId !== logsRequestIdRef.current) return;
      setLogs("Failed to connect to backend.");
    }
    if (reqId !== logsRequestIdRef.current) return;
    setLogsLoading(false);
  }, []);

  const showLogs = useCallback((containerId: string) => {
    setSelectedId(containerId);
    setView("logs");
    setLogs("");
    setKeyword("");
    setCurrentMatchRaw(0);
    setRangeFrom("");
    setRangeTo("");
    setRangeActive(false);
    setRangeError(null);
    followRef.current = true;
    // Stream takes over from here; no initial fetch needed.
  }, []);

  const applyRange = useCallback(() => {
    if (!selectedId) return;
    if (!rangeFrom || !rangeTo) {
      setRangeError("Both from and to are required");
      return;
    }
    const fromDate = new Date(rangeFrom);
    const toDate = new Date(rangeTo);
    if (isNaN(fromDate.getTime()) || isNaN(toDate.getTime())) {
      setRangeError("Invalid date");
      return;
    }
    if (fromDate >= toDate) {
      setRangeError("'from' must be before 'to'");
      return;
    }
    setRangeError(null);
    setRangeActive(true);
    fetchLogs(selectedId, TAIL_MAX, false, fromDate.toISOString(), toDate.toISOString());
  }, [selectedId, rangeFrom, rangeTo, fetchLogs]);

  const clearRange = useCallback(() => {
    if (!selectedId) return;
    setRangeFrom("");
    setRangeTo("");
    setRangeActive(false);
    setRangeError(null);
    setLogs("");
    followRef.current = true;
    // Streaming will resume automatically via the useLogStream hook.
  }, [selectedId]);

  useEffect(() => {
    if (logs === null || !logsRef.current) return;
    if (preserveScrollRef.current) {
      const delta = logsRef.current.scrollHeight - prevScrollHeightRef.current;
      logsRef.current.scrollTop = prevScrollTopRef.current + delta;
      preserveScrollRef.current = false;
      return;
    }
    if (rangeActive) {
      // Historical view: jump to bottom on each fetch (mirrors prior behavior).
      logsRef.current.scrollTop = logsRef.current.scrollHeight;
      return;
    }
    if (followRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight;
    }
  }, [logs, rangeActive]);

  const handleLogScroll = useCallback(() => {
    const el = logsRef.current;
    if (!el) return;
    // Follow when within 50px of bottom; pause when the user scrolls up.
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
  }, []);

  const highlighted = useMemo(() => {
    if (!logs || !keyword) return null;
    const lower = keyword.toLowerCase();
    const logsLower = logs.toLowerCase();
    const nodes: (string | { text: string; idx: number })[] = [];
    let running = 0;
    let cursor = 0;
    while (cursor < logs.length) {
      const hit = logsLower.indexOf(lower, cursor);
      if (hit === -1) {
        nodes.push(logs.slice(cursor));
        break;
      }
      if (hit > cursor) nodes.push(logs.slice(cursor, hit));
      nodes.push({ text: logs.slice(hit, hit + keyword.length), idx: running });
      running++;
      cursor = hit + keyword.length;
    }
    return { nodes, totalMatches: running };
  }, [logs, keyword]);

  const totalMatches = highlighted?.totalMatches ?? 0;
  const currentMatch = totalMatches > 0 ? Math.min(currentMatchRaw, totalMatches - 1) : 0;

  useEffect(() => {
    if (!scrollToMatchRef.current) return;
    const el = matchElsRef.current[currentMatch];
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    scrollToMatchRef.current = false;
  }, [currentMatch, highlighted]);

  const jumpToMatch = useCallback((delta: number) => {
    if (totalMatches === 0) return;
    scrollToMatchRef.current = true;
    setCurrentMatchRaw((prev) => {
      const clamped = Math.min(prev, totalMatches - 1);
      return (clamped + delta + totalMatches) % totalMatches;
    });
  }, [totalMatches]);

  if (containers.length === 0) {
    return (
      <div className="docker-empty">
        <p className="docker-empty-title">No containers found</p>
        <p className="docker-empty-sub">Ensure Docker is running and GLASSOPS_ENABLE_DOCKER=true</p>
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
          {!rangeActive && (
            <span className="docker-logs-stream-status" data-status={stream.status}>
              {stream.status === "streaming" && "● Live"}
              {stream.status === "connecting" && "Connecting…"}
              {stream.status === "ended" && "Ended"}
              {stream.status === "error" && (stream.error ?? "Error")}
            </span>
          )}
          {rangeActive && (
            <span className="docker-logs-range-badge">Date range</span>
          )}
        </div>
        <div className="docker-logs-toolbar">
          <div className="docker-logs-search">
            <Search size={12} className="docker-logs-search-icon" />
            <input
              type="text"
              className="docker-logs-search-input"
              placeholder="Search logs..."
              value={keyword}
              onChange={(e) => { setKeyword(e.target.value); setCurrentMatchRaw(0); }}
              onKeyDown={(e) => {
                if (e.key === "Enter") jumpToMatch(e.shiftKey ? -1 : 1);
                else if (e.key === "Escape") { setKeyword(""); setCurrentMatchRaw(0); }
              }}
              aria-label="Search logs"
            />
            {keyword && (
              <button
                className="docker-logs-search-clear"
                onClick={() => setKeyword("")}
                aria-label="Clear search"
              >
                <X size={12} />
              </button>
            )}
          </div>
          {keyword && (
            <>
              <span className="docker-logs-match-count">
                {totalMatches > 0 ? `${currentMatch + 1} / ${totalMatches}` : "0 matches"}
              </span>
              <button
                className="docker-logs-nav-btn"
                onClick={() => jumpToMatch(-1)}
                disabled={totalMatches === 0}
                aria-label="Previous match"
              >
                <ChevronUp size={12} />
              </button>
              <button
                className="docker-logs-nav-btn"
                onClick={() => jumpToMatch(1)}
                disabled={totalMatches === 0}
                aria-label="Next match"
              >
                <ChevronDown size={12} />
              </button>
            </>
          )}
        </div>
        <div className="docker-logs-toolbar">
          <Calendar size={12} className="docker-logs-range-icon" />
          <input
            type="datetime-local"
            className="docker-logs-range-input"
            value={rangeFrom}
            onChange={(e) => { setRangeFrom(e.target.value); setRangeError(null); }}
            aria-label="Range from"
          />
          <span className="docker-logs-range-sep">→</span>
          <input
            type="datetime-local"
            className="docker-logs-range-input"
            value={rangeTo}
            onChange={(e) => { setRangeTo(e.target.value); setRangeError(null); }}
            aria-label="Range to"
          />
          <button
            className="docker-logs-range-btn"
            onClick={applyRange}
            disabled={logsLoading || !rangeFrom || !rangeTo}
          >
            Apply
          </button>
          {rangeActive && (
            <button className="docker-logs-range-btn" onClick={clearRange} disabled={logsLoading}>
              Clear
            </button>
          )}
          {rangeError && <span className="docker-logs-range-error">{rangeError}</span>}
        </div>
        <pre ref={logsRef} className="docker-logs-content" onScroll={handleLogScroll}>
          {logsLoading && !logs ? "Loading logs..." : highlighted ? (
            highlighted.nodes.map((node, i) =>
              typeof node === "string" ? (
                <span key={i}>{node}</span>
              ) : (
                <mark
                  key={i}
                  ref={(el) => { matchElsRef.current[node.idx] = el; }}
                  className={`docker-logs-mark${node.idx === currentMatch ? " docker-logs-mark-current" : ""}`}
                >
                  {node.text}
                </mark>
              )
            )
          ) : logs}
        </pre>
      </div>
    );
  }

  return (
    <div>
      {actionError && <div className="docker-error" onClick={() => setActionError(null)}>{actionError}</div>}
      <table className="docker-table">
        <thead>
          <tr><th>Name</th><th>Image</th><th>Status</th><th>CPU</th><th>Memory</th><th>Ports</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {containers.map((c) => (
            <tr key={c.id} className={c.status === "running" ? "" : "docker-row-stopped"}>
              <td className="docker-cell-name">{c.name}</td>
              <td className="docker-cell-image">{c.image}</td>
              <td><StatusBadge status={c.status} /></td>
              <td className="docker-cell-num">{c.cpu_percent.toFixed(1)}%</td>
              <td className="docker-cell-num">{c.mem_usage > 0 ? `${formatBytes(c.mem_usage)} / ${formatBytes(c.mem_limit)}` : "—"}</td>
              <td className="docker-cell-ports">{c.ports.length > 0 ? c.ports.join(", ") : "—"}</td>
              <td className="docker-cell-actions">
                {c.status === "running" ? (
                  <>
                    <button className="docker-action-btn docker-action-stop" onClick={() => doAction(c.id, "stop")} disabled={actionLoading === c.id} title="Stop"><Square size={13} /></button>
                    <button className="docker-action-btn docker-action-restart" onClick={() => doAction(c.id, "restart")} disabled={actionLoading === c.id} title="Restart"><RotateCw size={13} /></button>
                  </>
                ) : (
                  <button className="docker-action-btn docker-action-start" onClick={() => doAction(c.id, "start")} disabled={actionLoading === c.id} title="Start"><Play size={13} /></button>
                )}
                <button className="docker-action-btn" onClick={() => showLogs(c.id)} title="Logs"><FileText size={13} /></button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
