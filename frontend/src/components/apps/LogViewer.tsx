import { useState, useEffect, useMemo, useRef } from "react";
import { Search, RefreshCw, ArrowDown } from "lucide-react";
import { fetchWithAuth } from "../../utils/api";

interface LogSource {
  type: string;
  name: string;
  path?: string;
  container_id?: string;
}

interface LogResult {
  queryKey: string;
  queryVersion: number;
  refreshVersion: number;
  lines: string[];
}

export default function LogViewer() {
  const [sources, setSources] = useState<LogSource[]>([]);
  const [selectedSource, setSelectedSource] = useState<LogSource | null>(null);
  const [search, setSearch] = useState("");
  const [result, setResult] = useState<LogResult>({
    queryKey: "",
    queryVersion: -1,
    refreshVersion: -1,
    lines: [],
  });
  const [queryVersion, setQueryVersion] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [autoScroll, setAutoScroll] = useState(true);
  const logRef = useRef<HTMLPreElement>(null);
  const queryKey = selectedSource
    ? `${selectedSource.type}\u0000${selectedSource.container_id ?? selectedSource.name}\u0000${search}`
    : "";
  const resultIsCurrent = result.queryKey === queryKey
    && result.queryVersion === queryVersion;
  const lines = useMemo(
    () => (resultIsCurrent ? result.lines : []),
    [result.lines, resultIsCurrent],
  );
  const loading = Boolean(selectedSource) && (
    !resultIsCurrent || result.refreshVersion !== refreshVersion
  );

  // Fetch sources
  useEffect(() => {
    fetchWithAuth("/api/logs/sources")
      .then((r) => r.json())
      .then((payload) => {
        const nextSources: LogSource[] = Array.isArray(payload.sources) ? payload.sources : [];
        setSources(nextSources);
        setSelectedSource((previous) => previous ?? nextSources[0] ?? null);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!selectedSource || !queryKey) return;
    let cancelled = false;

    const params = new URLSearchParams({
      source_type: selectedSource.type,
      name: selectedSource.type === "docker"
        ? (selectedSource.container_id || selectedSource.name)
        : selectedSource.name,
      tail: "500",
      search,
    });

    fetchWithAuth(`/api/logs/read?${params}`)
      .then(async (response) => {
        if (response.ok) {
          const payload = await response.json();
          return payload.lines?.length ? payload.lines : ["No logs available"];
        }
        if (response.status === 403) return ["Permission denied — log file is not readable"];
        if (response.status === 404) return ["Log source not found"];
        return [`Error: ${response.status}`];
      })
      .catch(() => ["Failed to load logs"])
      .then((nextLines: string[]) => {
        if (!cancelled) {
          setResult({ queryKey, queryVersion, refreshVersion, lines: nextLines });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [queryKey, queryVersion, refreshVersion, search, selectedSource]);

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  // Auto-refresh every 5s
  useEffect(() => {
    const timer = setInterval(() => {
      setRefreshVersion((version) => version + 1);
    }, 5000);
    return () => clearInterval(timer);
  }, [queryKey, queryVersion]);

  const highlightLine = (line: string) => {
    if (/error|fatal|panic/i.test(line)) return "log-line-error";
    if (/warn/i.test(line)) return "log-line-warn";
    if (/info/i.test(line)) return "log-line-info";
    return "";
  };

  return (
    <div className="log-viewer">
      {/* Toolbar */}
      <div className="log-toolbar">
        <select
          className="log-source-select"
          value={selectedSource ? `${selectedSource.type}:${selectedSource.name}` : ""}
          onChange={(e) => {
            const src = sources.find(
              (s) => `${s.type}:${s.name}` === e.target.value
            );
            if (src) {
              setSelectedSource(src);
              setQueryVersion((version) => version + 1);
            }
          }}
        >
          {sources.map((s) => (
            <option key={`${s.type}:${s.name}`} value={`${s.type}:${s.name}`}>
              [{s.type}] {s.name}
            </option>
          ))}
        </select>

        <div className="log-search">
          <Search size={13} />
          <input
            type="text"
            placeholder="Search..."
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setQueryVersion((version) => version + 1);
            }}
            className="log-search-input"
          />
        </div>

        <button
          className="log-toolbar-btn"
          onClick={() => setRefreshVersion((version) => version + 1)}
          title="Refresh"
        >
          <RefreshCw size={13} className={loading ? "log-spin" : ""} />
        </button>
        <button
          className={`log-toolbar-btn ${autoScroll ? "log-toolbar-btn-active" : ""}`}
          onClick={() => setAutoScroll(!autoScroll)}
          title="Auto-scroll"
        >
          <ArrowDown size={13} />
        </button>
      </div>

      {/* Log content */}
      <pre className="log-content" ref={logRef}>
        {lines.length === 0 ? (
          <span className="log-empty">No logs available</span>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={`log-line ${highlightLine(line)}`}>
              {line}
            </div>
          ))
        )}
      </pre>
    </div>
  );
}
