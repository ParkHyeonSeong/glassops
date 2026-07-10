import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import type { SetStateAction } from "react";
import { Search, ChevronUp, ChevronDown, X, Calendar } from "lucide-react";
import { useMetricsStore } from "../../stores/metricsStore";
import { fetchWithAuth } from "../../utils/api";
import { useLogStream } from "../../hooks/useLogStream";
import { StatusBadge, ContainerActionButtons, ContainerRemovedBanner } from "./dockerShared";
import { useContainerAction, type ContainerWindowProps } from "./dockerSharedUtils";

const TAIL_INITIAL = 300;
const TAIL_MAX = 2000;

interface ContainerLogBuffer {
  containerId: string | null;
  logs: string | null;
}

function useContainerLogBuffer(containerId: string | null) {
  const activeContainerIdRef = useRef(containerId);
  useEffect(() => {
    activeContainerIdRef.current = containerId;
  }, [containerId]);
  const [buffer, setBuffer] = useState<ContainerLogBuffer>({
    containerId,
    logs: null,
  });

  const logs = buffer.containerId === containerId ? buffer.logs : null;
  const updateLogs = useCallback((next: SetStateAction<string | null>) => {
    const sourceContainerId = containerId;
    if (sourceContainerId !== activeContainerIdRef.current) return;

    setBuffer((previousBuffer) => {
      if (sourceContainerId !== activeContainerIdRef.current) return previousBuffer;
      const previousLogs = previousBuffer.containerId === sourceContainerId
        ? previousBuffer.logs
        : null;
      const nextLogs = typeof next === "function"
        ? next(previousLogs)
        : next;
      return { containerId: sourceContainerId, logs: nextLogs };
    });
  }, [containerId]);

  return [logs, updateLogs] as const;
}

export default function ContainerLogsWindow({ agentId, containerName }: ContainerWindowProps) {
  // Resolve the live container against THIS window's agent — not the currently
  // selected one — so MenuBar host-switches don't poison opened windows. Name
  // is the stable key (id changes across container recreate; name survives).
  const container = useMetricsStore(
    (s) => s.agents[agentId]?.current?.containers?.find((c) => c.name === containerName),
  );
  const containerId = container?.id ?? null;
  const removed = !container;

  const [logs, setLogs] = useContainerLogBuffer(containerId);
  const [logsLoading, setLogsLoading] = useState(false);
  const action = useContainerAction(agentId, containerId);
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
  const followRef = useRef(true);

  const handleLogChunk = useCallback((chunk: string) => {
    setLogs((prev) => (prev ?? "") + chunk);
  }, [setLogs]);

  const streamEnabled = !!containerId && !rangeActive && !removed;
  const stream = useLogStream({
    containerId: streamEnabled ? containerId : null,
    agentId,
    tail: TAIL_INITIAL,
    enabled: streamEnabled,
    onLine: handleLogChunk,
  });

  const fetchLogs = useCallback(async (
    cid: string,
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
      const res = await fetchWithAuth(`/api/docker/containers/${cid}/logs?${params.toString()}`);
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
  }, [setLogs]);

  const applyRange = useCallback(() => {
    if (!containerId) return;
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
    fetchLogs(containerId, TAIL_MAX, false, fromDate.toISOString(), toDate.toISOString());
  }, [containerId, rangeFrom, rangeTo, fetchLogs]);

  const clearRange = useCallback(() => {
    if (!containerId) return;
    setRangeFrom("");
    setRangeTo("");
    setRangeActive(false);
    setRangeError(null);
    setLogs("");
    followRef.current = true;
  }, [containerId, setLogs]);

  useEffect(() => {
    if (logs === null || !logsRef.current) return;
    if (preserveScrollRef.current) {
      const delta = logsRef.current.scrollHeight - prevScrollHeightRef.current;
      logsRef.current.scrollTop = prevScrollTopRef.current + delta;
      preserveScrollRef.current = false;
      return;
    }
    if (rangeActive) {
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

  // Trim stale slots after the rendered marks have updated their refs.
  // Old slots beyond `totalMatches` are dead pointers we shouldn't scroll to.
  useEffect(() => {
    matchElsRef.current.length = totalMatches;
  }, [totalMatches]);

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

  return (
    <div className="docker-logs-view">
      <div className="docker-window-header">
        <span className="docker-logs-title">{containerName}</span>
        {container && <StatusBadge status={container.status} />}
        {!rangeActive && container && (
          <span className="docker-logs-stream-status" data-status={stream.status}>
            {stream.status === "streaming" && "● Live"}
            {stream.status === "connecting" && "Connecting…"}
            {stream.status === "ended" && "Ended"}
            {stream.status === "error" && (stream.error ?? "Error")}
          </span>
        )}
        {rangeActive && <span className="docker-logs-range-badge">Date range</span>}
        <div className="docker-window-actions">
          <ContainerActionButtons
            status={container?.status}
            removed={removed}
            loading={action.loading}
            onAction={action.run}
          />
        </div>
      </div>

      {removed && <ContainerRemovedBanner containerName={containerName} />}
      {action.error && (
        <div className="docker-error" onClick={action.clearError}>{action.error}</div>
      )}

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
            <button className="docker-logs-search-clear" onClick={() => setKeyword("")} aria-label="Clear search">
              <X size={12} />
            </button>
          )}
        </div>
        {keyword && (
          <>
            <span className="docker-logs-match-count">
              {totalMatches > 0 ? `${currentMatch + 1} / ${totalMatches}` : "0 matches"}
            </span>
            <button className="docker-logs-nav-btn" onClick={() => jumpToMatch(-1)}
              disabled={totalMatches === 0} aria-label="Previous match">
              <ChevronUp size={12} />
            </button>
            <button className="docker-logs-nav-btn" onClick={() => jumpToMatch(1)}
              disabled={totalMatches === 0} aria-label="Next match">
              <ChevronDown size={12} />
            </button>
          </>
        )}
      </div>

      <div className="docker-logs-toolbar">
        <Calendar size={12} className="docker-logs-range-icon" />
        <input type="datetime-local" className="docker-logs-range-input" value={rangeFrom}
          onChange={(e) => { setRangeFrom(e.target.value); setRangeError(null); }}
          aria-label="Range from" />
        <span className="docker-logs-range-sep">→</span>
        <input type="datetime-local" className="docker-logs-range-input" value={rangeTo}
          onChange={(e) => { setRangeTo(e.target.value); setRangeError(null); }}
          aria-label="Range to" />
        <button className="docker-logs-range-btn" onClick={applyRange}
          disabled={logsLoading || !rangeFrom || !rangeTo || removed}>
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
              <mark key={i}
                ref={(el) => { matchElsRef.current[node.idx] = el; }}
                className={`docker-logs-mark${node.idx === currentMatch ? " docker-logs-mark-current" : ""}`}>
                {node.text}
              </mark>
            )
          )
        ) : logs}
      </pre>
    </div>
  );
}
