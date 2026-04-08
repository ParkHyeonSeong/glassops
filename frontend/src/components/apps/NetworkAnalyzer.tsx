import { useMemo, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import { ArrowUp, ArrowDown } from "lucide-react";
import { useMetricsStore } from "../../stores/metricsStore";

function formatRate(bytesPerSec: number): string {
  if (bytesPerSec < 1024) return `${bytesPerSec} B/s`;
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`;
  return `${(bytesPerSec / (1024 * 1024)).toFixed(1)} MB/s`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export default function NetworkAnalyzer() {
  const current = useMetricsStore((s) => s.current);
  const history = useMetricsStore((s) => s.history);
  const connected = useMetricsStore((s) => s.connected);
  const [tab, setTab] = useState<"traffic" | "connections">("traffic");

  const net = current?.network;

  const chartData = useMemo(() => {
    return history.map((m, i) => ({
      t: String(i),
      send: m.network?.rates.send_rate ?? 0,
      recv: m.network?.rates.recv_rate ?? 0,
    }));
  }, [history]);

  if (!connected || !net) {
    return (
      <div className="net-empty">
        <p className="net-empty-title">
          {connected ? "Waiting for data..." : "Connecting to server..."}
        </p>
      </div>
    );
  }

  return (
    <div className="net-analyzer">
      {/* Rate cards */}
      <div className="net-rates">
        <div className="net-rate-card">
          <ArrowUp size={14} className="net-rate-icon net-rate-up" />
          <div>
            <div className="net-rate-value">{formatRate(net.rates.send_rate)}</div>
            <div className="net-rate-label">Upload</div>
          </div>
          <div className="net-rate-total">
            {formatBytes(net.io.bytes_sent ?? 0)} total
          </div>
        </div>
        <div className="net-rate-card">
          <ArrowDown size={14} className="net-rate-icon net-rate-down" />
          <div>
            <div className="net-rate-value">{formatRate(net.rates.recv_rate)}</div>
            <div className="net-rate-label">Download</div>
          </div>
          <div className="net-rate-total">
            {formatBytes(net.io.bytes_recv ?? 0)} total
          </div>
        </div>
        <div className="net-rate-card">
          <div className="net-rate-value">{net.connection_count}</div>
          <div className="net-rate-label">Connections</div>
        </div>
        {net.interfaces.map((iface) => (
          <div key={iface.name} className="net-rate-card">
            <div className="net-rate-value">{iface.name}</div>
            <div className="net-rate-label">
              {iface.ip} · {iface.is_up ? "Up" : "Down"}
              {iface.speed > 0 && ` · ${iface.speed}Mbps`}
            </div>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="net-tabs">
        <button
          className={`net-tab ${tab === "traffic" ? "net-tab-active" : ""}`}
          onClick={() => setTab("traffic")}
        >
          Traffic
        </button>
        <button
          className={`net-tab ${tab === "connections" ? "net-tab-active" : ""}`}
          onClick={() => setTab("connections")}
        >
          Connections ({net.connection_count})
        </button>
      </div>

      {/* Content */}
      {tab === "traffic" ? (
        <div className="net-chart">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              <defs>
                <linearGradient id="net-grad-send" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.02} />
                </linearGradient>
                <linearGradient id="net-grad-recv" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--color-success)" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="var(--color-success)" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" hide />
              <YAxis hide />
              <Tooltip
                contentStyle={{
                  background: "rgba(20,20,40,0.9)",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  fontSize: 12,
                  color: "#e0e0e0",
                }}
                formatter={(v, name) => [
                  formatRate(Number(v)),
                  name === "send" ? "Upload" : "Download",
                ]}
                labelFormatter={() => ""}
              />
              <Area
                type="monotone"
                dataKey="send"
                stroke="var(--color-accent)"
                strokeWidth={1.5}
                fill="url(#net-grad-send)"
                isAnimationActive={false}
              />
              <Area
                type="monotone"
                dataKey="recv"
                stroke="var(--color-success)"
                strokeWidth={1.5}
                fill="url(#net-grad-recv)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="net-connections">
          <table className="net-conn-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Local</th>
                <th>Remote</th>
                <th>Status</th>
                <th>PID</th>
              </tr>
            </thead>
            <tbody>
              {net.connections.slice(0, 50).map((conn) => (
                <tr key={`${conn.type}-${conn.laddr}-${conn.raddr}-${conn.pid}`}>
                  <td className="net-conn-type">{conn.type}</td>
                  <td className="net-conn-addr">{conn.laddr}</td>
                  <td className="net-conn-addr">{conn.raddr || "—"}</td>
                  <td>
                    <span className={`net-conn-status net-conn-${conn.status === "ESTABLISHED" ? "est" : "other"}`}>
                      {conn.status}
                    </span>
                  </td>
                  <td className="net-conn-pid">{conn.pid ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
