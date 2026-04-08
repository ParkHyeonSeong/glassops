import { useState, useEffect, useRef, useCallback } from "react";
import { Search, RefreshCw, ArrowDown } from "lucide-react";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

interface LogSource {
  type: string;
  name: string;
  path?: string;
  container_id?: string;
}

export default function LogViewer() {
  const [sources, setSources] = useState<LogSource[]>([]);
  const [selectedSource, setSelectedSource] = useState<LogSource | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const logRef = useRef<HTMLPreElement>(null);

  // Fetch sources
  useEffect(() => {
    fetch(`${BACKEND_URL}/api/logs/sources`)
      .then((r) => r.json())
      .then((d) => {
        setSources(d.sources || []);
        if (d.sources?.length > 0 && !selectedSource) {
          setSelectedSource(d.sources[0]);
        }
      })
      .catch(() => {});
  }, []);

  const fetchLogs = useCallback(async () => {
    if (!selectedSource) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        source_type: selectedSource.type,
        name: selectedSource.type === "docker"
          ? (selectedSource.container_id || selectedSource.name)
          : selectedSource.name,
        tail: "500",
        search,
      });
      const res = await fetch(`${BACKEND_URL}/api/logs/read?${params}`);
      if (res.ok) {
        const data = await res.json();
        setLines(data.lines?.length ? data.lines : ["No logs available"]);
      } else if (res.status === 403) {
        setLines(["Permission denied — log file is not readable"]);
      } else if (res.status === 404) {
        setLines(["Log source not found"]);
      } else {
        setLines([`Error: ${res.status}`]);
      }
    } catch {
      setLines(["Failed to load logs"]);
    }
    setLoading(false);
  }, [selectedSource, search]);

  // Fetch on source/search change
  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  // Auto-refresh every 5s
  useEffect(() => {
    const timer = setInterval(fetchLogs, 5000);
    return () => clearInterval(timer);
  }, [fetchLogs]);

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
              setLines([]);
              setSelectedSource(src);
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
            onChange={(e) => setSearch(e.target.value)}
            className="log-search-input"
          />
        </div>

        <button className="log-toolbar-btn" onClick={fetchLogs} title="Refresh">
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
