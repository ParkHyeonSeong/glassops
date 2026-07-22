import { useState, useEffect } from "react";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAuthStore } from "../../stores/authStore";
import { useSettingsStore, WALLPAPERS } from "../../stores/settingsStore";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { type AlertMetricKey } from "../../lib/thresholds";
import { fetchWithAuth } from "../../utils/api";

type Tab = "profile" | "agents" | "server" | "alerts" | "email" | "appearance";

export default function SettingsApp() {
  const [tab, setTab] = useState<Tab>("profile");
  const email = useAuthStore((s) => s.email);
  const agentId = useMetricsStore((s) => s.agentId);
  const connected = useMetricsStore((s) => s.connected);
  const logout = useAuthStore((s) => s.logout);

  return (
    <div className="settings-app">
      <div className="settings-sidebar">
        {(["profile", "agents", "server", "alerts", "email", "appearance"] as Tab[]).map((t) => (
          <button key={t} className={`settings-nav ${tab === t ? "settings-nav-active" : ""}`}
            onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      <div className="settings-content">
        {tab === "profile" && <ProfileTab email={email} onLogout={logout} />}
        {tab === "agents" && <AgentsTab agentId={agentId} connected={connected} />}
        {tab === "server" && <ServerTab />}
        {tab === "alerts" && <AlertsTab />}
        {tab === "email" && <EmailTab />}
        {tab === "appearance" && <AppearanceTab />}
      </div>
    </div>
  );
}

function ProfileTab({ email, onLogout }: { email: string | null; onLogout: () => void }) {
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [msg, setMsg] = useState("");
  const handleChangePw = async () => {
    setMsg("");
    const res = await fetchWithAuth("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
    });
    if (res.ok) {
      setMsg("Password changed successfully");
      setOldPw(""); setNewPw("");
    } else {
      const d = await res.json().catch(() => ({}));
      setMsg(d.detail || "Failed");
    }
  };

  return (
    <div className="settings-section">
      <h3 className="settings-title">Profile</h3>
      <div className="settings-field">
        <label className="settings-label">Email</label>
        <span className="settings-value">{email ?? "—"}</span>
      </div>

      <h4 className="settings-subtitle">Change Password</h4>
      <input type="password" placeholder="Current password" value={oldPw}
        onChange={(e) => setOldPw(e.target.value)} className="settings-input" />
      <input type="password" placeholder="New password" value={newPw}
        onChange={(e) => setNewPw(e.target.value)} className="settings-input" />
      <button className="settings-btn" onClick={handleChangePw}
        disabled={!oldPw || !newPw}>Change Password</button>
      {msg && <p className="settings-msg">{msg}</p>}

      <div style={{ marginTop: 20 }}>
        <button className="settings-btn settings-btn-danger" onClick={onLogout}>Sign Out</button>
      </div>
    </div>
  );
}

function AgentsTab({ agentId, connected }: { agentId: string | null; connected: boolean }) {
  return (
    <div className="settings-section">
      <h3 className="settings-title">Connected Agents</h3>
      <div className="settings-agent-card">
        <div className="settings-agent-info">
          <span className="settings-agent-name">{agentId ?? "None"}</span>
          <span className="settings-agent-status"
            style={{ color: connected ? "var(--color-success)" : "var(--color-danger)" }}>
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>
      <div className="settings-hint">
        To add remote agents, install the GlassOps Agent on the target server
        and point GLASSOPS_SERVER_URL to this instance.
      </div>
    </div>
  );
}

const ALERT_ROWS: { key: AlertMetricKey; label: string }[] = [
  { key: "cpu", label: "CPU" },
  { key: "mem", label: "Memory" },
  { key: "disk", label: "Disk" },
];

const BOUNDS = [
  { bound: "warn", suffix: "Warning" },
  { bound: "crit", suffix: "Critical" },
] as const;

function AlertsTab() {
  const thresholds = useThresholdsStore((s) => s.thresholds);
  const setThreshold = useThresholdsStore((s) => s.setThreshold);

  return (
    <div className="settings-section">
      <h3 className="settings-title">Alert Thresholds</h3>
      <p className="settings-hint">
        Applies to in-browser alerts only — desktop toasts and the System Monitor
        banner and feed. Email alerts use their own server-side thresholds, set
        under Settings &gt; Email.
      </p>
      {ALERT_ROWS.flatMap(({ key, label }) =>
        BOUNDS.map(({ bound, suffix }) => {
          const id = `threshold-${key}-${bound}`;
          return (
            <div key={id} className="settings-slider-row">
              <label className="settings-label" htmlFor={id}>{label} {suffix}</label>
              <input id={id} type="range" min="0" max="100"
                value={thresholds[key][bound]}
                onChange={(e) =>
                  setThreshold(key, { ...thresholds[key], [bound]: Number(e.target.value) })}
                className="settings-range" />
              <span className="settings-range-value">{thresholds[key][bound]}%</span>
            </div>
          );
        }),
      )}
    </div>
  );
}

function ServerTab() {
  const [config, setConfig] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [restarting, setRestarting] = useState(false);
  const [showConfirm, setShowConfirm] = useState<string | null>(null);

  useEffect(() => {
    fetchWithAuth("/api/settings/runtime").then((r) => r.json()).then((d) => {
      setConfig(d.config || {});
      setLoaded(true);
    }).catch(() => setLoaded(true));
  }, []);

  const update = (key: string, value: string) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
    setMsg("");
  };

  const handleSave = async () => {
    setMsg("");
    const res = await fetchWithAuth("/api/settings/runtime", {
      method: "POST",
      body: JSON.stringify(config),
    });
    if (res.ok) {
      setDirty(false);
      setMsg("Saved. Click Apply to restart services.");
    } else {
      const d = await res.json().catch(() => ({}));
      setMsg(d.detail || "Save failed");
    }
  };

  const handleApply = async (service: string) => {
    setShowConfirm(null);
    setRestarting(true);
    setMsg(`Restarting ${service}...`);
    const res = await fetchWithAuth("/api/settings/restart", {
      method: "POST",
      body: JSON.stringify({ service }),
    });
    setRestarting(false);
    if (res.ok) {
      setMsg(`${service} restarted successfully`);
    } else {
      const d = await res.json().catch(() => ({}));
      setMsg(d.detail || "Restart failed");
    }
  };

  if (!loaded) return <p className="settings-hint">Loading...</p>;

  const toggles = [
    { key: "enable_gpu", label: "GPU Monitoring" },
    { key: "enable_docker", label: "Docker Monitoring" },
  ];

  return (
    <div className="settings-section">
      <h3 className="settings-title">Server Configuration</h3>

      {toggles.map((t) => (
        <div key={t.key} className="settings-toggle-row">
          <span className="settings-toggle-label">{t.label}</span>
          <button
            className={`settings-toggle ${config[t.key] === "true" ? "settings-toggle-on" : ""}`}
            onClick={() => update(t.key, config[t.key] === "true" ? "false" : "true")}
          >
            <span className="settings-toggle-knob" />
          </button>
        </div>
      ))}

      <div className="settings-field">
        <label className="settings-label">Collection Interval (seconds)</label>
        <input type="number" min="1" max="60" value={config.collect_interval || "1"}
          onChange={(e) => update("collect_interval", e.target.value)}
          className="settings-input" style={{ width: 80 }} />
      </div>

      <div className="settings-field">
        <label className="settings-label">Terminal User</label>
        <input type="text" value={config.terminal_user || ""}
          onChange={(e) => update("terminal_user", e.target.value)}
          placeholder="(login prompt)"
          className="settings-input" />
      </div>

      <div className="settings-field">
        <label className="settings-label">IP Whitelist (comma-separated, empty = all)</label>
        <input type="text" value={config.allowed_ips || ""}
          onChange={(e) => update("allowed_ips", e.target.value)}
          placeholder="10.0.0.0/8, 192.168.1.0/24"
          className="settings-input" />
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <button className="settings-btn" onClick={handleSave} disabled={!dirty}>
          Save
        </button>
        <button className="settings-btn" onClick={() => setShowConfirm("agent")} disabled={restarting}>
          Apply (Restart Agent)
        </button>
        {config.allowed_ips !== undefined && (
          <button className="settings-btn" onClick={() => setShowConfirm("nginx")} disabled={restarting}>
            Apply IP Rules (Restart Nginx)
          </button>
        )}
      </div>

      {msg && <p className="settings-msg">{msg}</p>}

      {/* Confirm dialog */}
      {showConfirm && (
        <div className="proc-kill-overlay" onClick={() => setShowConfirm(null)}>
          <div className="proc-kill-modal" onClick={(e) => e.stopPropagation()}>
            <p>Restart <strong>{showConfirm}</strong>?</p>
            <p className="settings-hint" style={{ marginTop: 4 }}>
              {showConfirm === "agent"
                ? "Metrics collection will pause for a few seconds."
                : "Active connections may be briefly interrupted."}
            </p>
            <div className="proc-kill-actions">
              <button className="settings-btn" onClick={() => setShowConfirm(null)}>Cancel</button>
              <button className="settings-btn settings-btn-danger" onClick={() => handleApply(showConfirm)}>Restart</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

interface EmailConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  from_email: string;
  to_email: string;
  use_tls: boolean;
  start_tls: boolean;
}

type EmailFieldKey = "host" | "port" | "username" | "password" | "from_email" | "to_email";

interface EmailField {
  key: EmailFieldKey;
  label: string;
  type?: "text" | "number" | "password";
}

function EmailTab() {
  const [config, setConfig] = useState<EmailConfig>({
    host: "",
    port: 587,
    username: "",
    password: "",
    from_email: "",
    to_email: "",
    use_tls: false,
    start_tls: true,
  });
  const [msg, setMsg] = useState("");
  const [loaded, setLoaded] = useState(false);

  const updateEmailField = (key: EmailFieldKey, rawValue: string) => {
    setConfig((previous) => (
      key === "port"
        ? { ...previous, port: Number(rawValue) }
        : { ...previous, [key]: rawValue }
    ));
  };

  useEffect(() => {
    fetchWithAuth("/api/alerts/config").then((r) => r.json()).then((d) => {
      if (d.configured) {
        setConfig((prev) => ({ ...prev, ...d }));
      }
      setLoaded(true);
    }).catch(() => setLoaded(true));
  }, []);

  const handleSave = async () => {
    setMsg("");
    const res = await fetchWithAuth("/api/alerts/config", {
      method: "POST",
      body: JSON.stringify(config),
    });
    setMsg(res.ok ? "Saved" : "Failed to save");
  };

  const handleTest = async () => {
    setMsg("Sending...");
    const res = await fetchWithAuth("/api/alerts/test", { method: "POST" });
    const d = await res.json().catch(() => ({}));
    setMsg(res.ok ? "Test email sent!" : d.detail || "Send failed");
  };

  if (!loaded) return <p className="settings-hint">Loading...</p>;

  const fields: EmailField[] = [
    { key: "host", label: "SMTP Host" },
    { key: "port", label: "Port", type: "number" },
    { key: "username", label: "Username" },
    { key: "password", label: "Password", type: "password" },
    { key: "from_email", label: "From Email" },
    { key: "to_email", label: "To Email (alerts)" },
  ];

  return (
    <div className="settings-section">
      <h3 className="settings-title">Email Alerts (SMTP)</h3>
      {fields.map((f) => (
        <div key={f.key} className="settings-field">
          <label className="settings-label">{f.label}</label>
          <input
            type={f.type || "text"}
            value={config[f.key]}
            onChange={(event) => updateEmailField(f.key, event.target.value)}
            className="settings-input"
          />
        </div>
      ))}
      <div className="settings-field">
        <label className="settings-label">
          <input type="checkbox" checked={config.start_tls}
            onChange={(e) => setConfig((prev) => ({ ...prev, start_tls: e.target.checked }))} />
          {" "}STARTTLS
        </label>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button className="settings-btn" onClick={handleSave}>Save</button>
        <button className="settings-btn" onClick={handleTest} disabled={!config.host}>Test Email</button>
      </div>
      {msg && <p className="settings-msg">{msg}</p>}
    </div>
  );
}

function AppearanceTab() {
  const { wallpaper, setWallpaper } = useSettingsStore();

  return (
    <div className="settings-section">
      <h3 className="settings-title">Wallpaper</h3>
      <div className="settings-wallpaper-grid">
        {WALLPAPERS.map((wp) => (
          <button key={wp.id}
            className={`settings-wallpaper-item ${wallpaper === wp.id ? "settings-wallpaper-active" : ""}`}
            onClick={() => setWallpaper(wp.id)}
            style={{ background: wp.css }}>
            <span className="settings-wallpaper-label">{wp.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
