import { useState, useMemo } from "react";
import { Search } from "lucide-react";
import { useMetricsStore } from "../../stores/metricsStore";

type SortKey = "cpu" | "mem" | "pid" | "name";

export default function ProcessViewer() {
  const current = useMetricsStore((s) => s.current);
  const connected = useMetricsStore((s) => s.connected);
  const [sortKey, setSortKey] = useState<SortKey>("cpu");
  const [sortAsc, setSortAsc] = useState(false);
  const [filter, setFilter] = useState("");

  const processes = current?.processes ?? [];

  const sorted = useMemo(() => {
    let list = [...processes];

    if (filter) {
      const q = filter.toLowerCase();
      list = list.filter(
        (p) =>
          p.name.toLowerCase().includes(q) ||
          p.user.toLowerCase().includes(q) ||
          String(p.pid).includes(q)
      );
    }

    list.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (typeof av === "string" && typeof bv === "string")
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      return sortAsc
        ? (av as number) - (bv as number)
        : (bv as number) - (av as number);
    });
    return list;
  }, [processes, sortKey, sortAsc, filter]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(false); }
  };

  const arrow = (key: SortKey) =>
    sortKey === key ? (sortAsc ? " ▲" : " ▼") : "";

  if (!connected || processes.length === 0) {
    return (
      <div className="proc-empty">
        <p>{connected ? "Waiting for data..." : "Connecting..."}</p>
      </div>
    );
  }

  return (
    <div className="proc-viewer">
      <div className="proc-toolbar">
        <div className="proc-search">
          <Search size={13} />
          <input
            type="text"
            placeholder="Filter processes..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="proc-search-input"
          />
        </div>
        <span className="proc-count">{sorted.length} processes</span>
      </div>

      <div className="proc-table-wrap">
        <table className="proc-table">
          <thead>
            <tr>
              <th onClick={() => handleSort("pid")} className="proc-th-sortable">PID{arrow("pid")}</th>
              <th onClick={() => handleSort("name")} className="proc-th-sortable">Name{arrow("name")}</th>
              <th onClick={() => handleSort("cpu")} className="proc-th-sortable">CPU%{arrow("cpu")}</th>
              <th onClick={() => handleSort("mem")} className="proc-th-sortable">MEM%{arrow("mem")}</th>
              <th>User</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr key={p.pid}>
                <td className="proc-cell-pid">{p.pid}</td>
                <td className="proc-cell-name">{p.name}</td>
                <td className="proc-cell-num">
                  <span className="proc-bar-wrap">
                    <span
                      className="proc-bar"
                      style={{
                        width: `${Math.min(p.cpu, 100)}%`,
                        background: p.cpu > 50 ? "var(--color-danger)" : "var(--color-accent)",
                      }}
                    />
                  </span>
                  {p.cpu.toFixed(1)}
                </td>
                <td className="proc-cell-num">
                  <span className="proc-bar-wrap">
                    <span
                      className="proc-bar"
                      style={{
                        width: `${Math.min(p.mem, 100)}%`,
                        background: p.mem > 50 ? "var(--color-warning)" : "var(--color-success)",
                      }}
                    />
                  </span>
                  {p.mem.toFixed(1)}
                </td>
                <td className="proc-cell-user">{p.user}</td>
                <td className="proc-cell-status">{p.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
