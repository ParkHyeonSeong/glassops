import { useState } from "react";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAuthStore } from "../../stores/authStore";
import { useSettingsStore, WALLPAPERS } from "../../stores/settingsStore";
import { fetchWithAuth } from "../../utils/api";

type Tab = "profile" | "agents" | "alerts" | "appearance";

export default function SettingsApp() {
  const [tab, setTab] = useState<Tab>("profile");
  const email = useAuthStore((s) => s.email);
  const agentId = useMetricsStore((s) => s.agentId);
  const connected = useMetricsStore((s) => s.connected);
  const logout = useAuthStore((s) => s.logout);

  return (
    <div className="settings-app">
      <div className="settings-sidebar">
        {(["profile", "agents", "alerts", "appearance"] as Tab[]).map((t) => (
          <button key={t} className={`settings-nav ${tab === t ? "settings-nav-active" : ""}`}
            onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      <div className="settings-content">
        {tab === "profile" && <ProfileTab email={email} onLogout={logout} />}
        {tab === "agents" && <AgentsTab agentId={agentId} connected={connected} />}
        {tab === "alerts" && <AlertsTab />}
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

function AlertsTab() {
  const { alertThresholds, setThreshold } = useSettingsStore();

  const sliders = [
    { key: "cpuWarn", label: "CPU Warning", unit: "%", max: 100 },
    { key: "cpuCrit", label: "CPU Critical", unit: "%", max: 100 },
    { key: "memWarn", label: "Memory Warning", unit: "%", max: 100 },
    { key: "memCrit", label: "Memory Critical", unit: "%", max: 100 },
    { key: "diskCrit", label: "Disk Critical", unit: "%", max: 100 },
  ];

  return (
    <div className="settings-section">
      <h3 className="settings-title">Alert Thresholds</h3>
      {sliders.map((s) => (
        <div key={s.key} className="settings-slider-row">
          <label className="settings-label">{s.label}</label>
          <input type="range" min="10" max={s.max}
            value={alertThresholds[s.key as keyof typeof alertThresholds]}
            onChange={(e) => setThreshold(s.key as "cpuWarn" | "cpuCrit" | "memWarn" | "memCrit" | "diskCrit", Number(e.target.value))}
            className="settings-range" />
          <span className="settings-range-value">{alertThresholds[s.key as keyof typeof alertThresholds]}{s.unit}</span>
        </div>
      ))}
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
