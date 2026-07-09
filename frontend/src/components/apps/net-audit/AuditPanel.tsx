import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import { useMetricsStore } from "../../../stores/metricsStore";
import { fetchWithAuth } from "../../../utils/api";

interface NetAuditEvent {
  ts: number;
  event: "open" | "close";
  proto: string;
  laddr: string;
  lport: number | null;
  raddr: string;
  rport: number | null;
  status: string;
  pid: number | null;
  pname: string;
  duration: number | null;
}

interface RollupInterface {
  name: string;
  bytes_in: number;
  bytes_out: number;
}

interface RollupTopTalker {
  raddr: string;
  conns: number;
}

interface NetAuditRollup {
  ts: number;
  interfaces: RollupInterface[];
  top_talkers: RollupTopTalker[];
}

type RollupDuration = "1h" | "6h" | "24h" | "7d" | "30d";

const EVENTS_PAGE_SIZE = 200;
const ADMIN_REQUIRED_NOTICE = "Admin access required.";
const DISABLED_NOTICE =
  "No network-audit data for this host. It may be disabled — set GLASSOPS_ENABLE_NET_AUDIT=true on its agent, or the host's netns may be unreadable.";

function formatBytesPerMin(bytes: number): string {
  if (bytes < 1024) return `${bytes.toFixed(0)} B/min`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB/min`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB/min`;
}

function formatAddr(addr: string, port: number | null): string {
  return port != null ? `${addr}:${port}` : addr;
}

function EmptyNotice({ text }: { text: string }) {
  return (
    <div className="net-empty">
      <p className="net-empty-title">{text}</p>
    </div>
  );
}

export default function AuditPanel() {
  const agentId = useMetricsStore((s) => s.agentId);

  // Shared: a 403 from either endpoint means the caller lost admin access mid-session
  // (the tab itself is already admin-gated in NetworkAnalyzer — this is defence-in-depth).
  const [notice, setNotice] = useState<string | null>(null);

  // ── Connection events ──
  const [events, setEvents] = useState<NetAuditEvent[]>([]);
  const [eventsLoaded, setEventsLoaded] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [raddrFilter, setRaddrFilter] = useState("");
  const [portFilter, setPortFilter] = useState("");
  const [pidFilter, setPidFilter] = useState("");

  const buildEventsUrl = useCallback((before?: number) => {
    const params = new URLSearchParams({ limit: String(EVENTS_PAGE_SIZE) });
    if (before !== undefined) params.set("before", String(before));
    if (raddrFilter.trim()) params.set("raddr", raddrFilter.trim());
    if (portFilter.trim()) params.set("port", portFilter.trim());
    if (pidFilter.trim()) params.set("pid", pidFilter.trim());
    return `/api/net-audit/${agentId}/events?${params.toString()}`;
  }, [agentId, raddrFilter, portFilter, pidFilter]);

  // Initial load / reload on agent or filter change.
  useEffect(() => {
    if (!agentId) return;
    let live = true;
    fetchWithAuth(buildEventsUrl())
      .then(async (res) => {
        if (!live) return;
        if (res.status === 403) {
          setNotice(ADMIN_REQUIRED_NOTICE);
          setEvents([]);
          setHasMore(false);
          return;
        }
        if (!res.ok) {
          setEvents([]);
          setHasMore(false);
          return;
        }
        setNotice(null);
        const data = await res.json();
        const rows: NetAuditEvent[] = data.events ?? [];
        setEvents(rows);
        setHasMore(rows.length >= EVENTS_PAGE_SIZE);
      })
      .catch(() => {
        if (!live) return;
        setEvents([]);
        setHasMore(false);
      })
      .finally(() => {
        if (live) setEventsLoaded(true);
      });
    return () => { live = false; };
  }, [agentId, buildEventsUrl]);

  const loadOlder = useCallback(async () => {
    if (!agentId || events.length === 0) return;
    setLoadingMore(true);
    try {
      const oldestTs = events[events.length - 1].ts;
      const res = await fetchWithAuth(buildEventsUrl(oldestTs));
      if (res.status === 403) {
        setNotice(ADMIN_REQUIRED_NOTICE);
        setHasMore(false);
        return;
      }
      if (!res.ok) {
        setHasMore(false);
        return;
      }
      const data = await res.json();
      const rows: NetAuditEvent[] = data.events ?? [];
      setEvents((prev) => [...prev, ...rows]);
      setHasMore(rows.length >= EVENTS_PAGE_SIZE);
    } catch {
      setHasMore(false);
    } finally {
      setLoadingMore(false);
    }
  }, [agentId, events, buildEventsUrl]);

  const clearFilters = useCallback(() => {
    setRaddrFilter("");
    setPortFilter("");
    setPidFilter("");
  }, []);

  // ── Bandwidth rollup ──
  const [rollups, setRollups] = useState<NetAuditRollup[]>([]);
  const [rollupLoaded, setRollupLoaded] = useState(false);
  const [duration, setDuration] = useState<RollupDuration>("24h");

  useEffect(() => {
    if (!agentId) return;
    let live = true;
    fetchWithAuth(`/api/net-audit/${agentId}/rollup?duration=${duration}`)
      .then(async (res) => {
        if (!live) return;
        if (res.status === 403) {
          setNotice(ADMIN_REQUIRED_NOTICE);
          setRollups([]);
          return;
        }
        if (!res.ok) {
          setRollups([]);
          return;
        }
        setNotice(null);
        const data = await res.json();
        setRollups(data.rollups ?? []);
      })
      .catch(() => {
        if (!live) return;
        setRollups([]);
      })
      .finally(() => {
        if (live) setRollupLoaded(true);
      });
    return () => { live = false; };
  }, [agentId, duration]);

  const chartData = useMemo(() => rollups.map((r) => ({
    t: r.ts,
    bytes_in: r.interfaces.reduce((sum, i) => sum + (i.bytes_in ?? 0), 0),
    bytes_out: r.interfaces.reduce((sum, i) => sum + (i.bytes_out ?? 0), 0),
  })), [rollups]);

  const topTalkers = useMemo(() => {
    if (rollups.length === 0) return [];
    const latest = rollups[rollups.length - 1];
    return [...(latest.top_talkers ?? [])].sort((a, b) => b.conns - a.conns);
  }, [rollups]);

  const formatTick = useCallback((ts: number) => {
    const d = new Date(ts * 1000);
    if (duration === "1h" || duration === "6h" || duration === "24h") {
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }, [duration]);

  if (!agentId) {
    return <EmptyNotice text="No host selected." />;
  }

  if (notice) {
    return <EmptyNotice text={notice} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, flex: 1, minHeight: 0, overflow: "auto" }}>
      {/* Connection events */}
      <div className="sysmon-chart-card" style={{ minHeight: 260 }}>
        <div className="gpu-controls" style={{ justifyContent: "space-between" }}>
          <span className="sysmon-chart-title">Connection Events</span>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div className="log-search" style={{ minWidth: 140 }}>
              <input
                className="log-search-input"
                placeholder="Remote addr"
                value={raddrFilter}
                onChange={(e) => setRaddrFilter(e.target.value)}
              />
            </div>
            <div className="log-search" style={{ maxWidth: 90 }}>
              <input
                className="log-search-input"
                placeholder="Port"
                inputMode="numeric"
                value={portFilter}
                onChange={(e) => setPortFilter(e.target.value.replace(/\D/g, ""))}
              />
            </div>
            <div className="log-search" style={{ maxWidth: 90 }}>
              <input
                className="log-search-input"
                placeholder="PID"
                inputMode="numeric"
                value={pidFilter}
                onChange={(e) => setPidFilter(e.target.value.replace(/\D/g, ""))}
              />
            </div>
            {(raddrFilter || portFilter || pidFilter) && (
              <button className="log-toolbar-btn" onClick={clearFilters} title="Clear filters">×</button>
            )}
          </div>
        </div>

        {!eventsLoaded ? (
          <EmptyNotice text="Loading…" />
        ) : events.length === 0 ? (
          <EmptyNotice text={DISABLED_NOTICE} />
        ) : (
          <div className="net-connections">
            <table className="net-conn-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Event</th>
                  <th>Proto</th>
                  <th>Local → Remote</th>
                  <th>Status</th>
                  <th>Process</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e, i) => (
                  <tr key={`${e.ts}-${e.event}-${e.laddr}-${e.lport}-${e.raddr}-${e.rport}-${e.pid}-${i}`}>
                    <td className="net-conn-addr">{new Date(e.ts * 1000).toLocaleString()}</td>
                    <td>
                      <span className={`net-conn-status ${e.event === "open" ? "net-conn-est" : "net-conn-other"}`}>
                        {e.event}
                      </span>
                    </td>
                    <td className="net-conn-type">{e.proto}</td>
                    <td className="net-conn-addr">
                      {formatAddr(e.laddr, e.lport)} → {formatAddr(e.raddr, e.rport)}
                    </td>
                    <td>{e.status || "—"}</td>
                    <td className="net-conn-addr">
                      {e.pname || "—"}{e.pid != null ? ` (${e.pid})` : ""}
                    </td>
                    <td className="net-conn-pid">{e.duration != null ? `${e.duration.toFixed(1)}s` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {hasMore && events.length > 0 && (
          <button className="docker-logs-load-more" onClick={loadOlder} disabled={loadingMore} style={{ alignSelf: "center" }}>
            {loadingMore ? "Loading…" : "Load older"}
          </button>
        )}
      </div>

      {/* Bandwidth rollup */}
      <div className="sysmon-chart-card" style={{ minHeight: 260 }}>
        <div className="gpu-controls" style={{ justifyContent: "space-between" }}>
          <span className="sysmon-chart-title">Interface Bandwidth</span>
          <div className="sysmon-tabs">
            {(["1h", "6h", "24h", "7d", "30d"] as RollupDuration[]).map((d) => (
              <button
                key={d}
                className={`sysmon-tab ${duration === d ? "sysmon-tab-active" : ""}`}
                onClick={() => setDuration(d)}
              >
                {d}
              </button>
            ))}
          </div>
        </div>

        {!rollupLoaded ? (
          <EmptyNotice text="Loading…" />
        ) : chartData.length === 0 ? (
          <EmptyNotice text={DISABLED_NOTICE} />
        ) : (
          <>
            <div className="net-chart">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                  <defs>
                    <linearGradient id="net-audit-grad-in" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--color-success)" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="var(--color-success)" stopOpacity={0.02} />
                    </linearGradient>
                    <linearGradient id="net-audit-grad-out" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <XAxis
                    dataKey="t"
                    tick={{ fontSize: 9, fill: "var(--text-secondary)" }}
                    tickLine={false}
                    axisLine={false}
                    interval="preserveStartEnd"
                    minTickGap={60}
                    tickFormatter={formatTick}
                  />
                  <YAxis
                    tick={{ fontSize: 9, fill: "var(--text-secondary)" }}
                    tickLine={false}
                    axisLine={false}
                    width={64}
                    tickFormatter={(v) => formatBytesPerMin(Number(v))}
                    label={{ value: "bytes/min", angle: -90, position: "insideLeft", fontSize: 9, fill: "var(--text-secondary)" }}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "rgba(20,20,40,0.9)",
                      border: "1px solid rgba(255,255,255,0.1)",
                      borderRadius: 8,
                      fontSize: 12,
                      color: "#e0e0e0",
                    }}
                    formatter={(v, name) => [
                      formatBytesPerMin(Number(v)),
                      name === "bytes_in" ? "In" : "Out",
                    ]}
                    labelFormatter={(ts) => new Date(Number(ts) * 1000).toLocaleString()}
                  />
                  <Area
                    type="monotone"
                    dataKey="bytes_in"
                    stroke="var(--color-success)"
                    strokeWidth={1.5}
                    fill="url(#net-audit-grad-in)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="bytes_out"
                    stroke="var(--color-accent)"
                    strokeWidth={1.5}
                    fill="url(#net-audit-grad-out)"
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div>
              <span className="sysmon-chart-title">Top Talkers</span>
              {topTalkers.length === 0 ? (
                <p style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>No data.</p>
              ) : (
                <ul style={{ listStyle: "none", margin: "4px 0 0", padding: 0 }}>
                  {topTalkers.slice(0, 10).map((t) => (
                    <li
                      key={t.raddr}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        padding: "3px 2px",
                        fontSize: 11,
                        borderBottom: "1px solid rgba(255,255,255,0.03)",
                      }}
                    >
                      <span className="net-conn-addr">{t.raddr}</span>
                      <span style={{ color: "var(--text-secondary)" }}>{t.conns} conns</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
