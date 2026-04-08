import { useState } from "react";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAuthStore } from "../../stores/authStore";

type Tab = "profile" | "agents" | "appearance";

export default function SettingsApp() {
  const [tab, setTab] = useState<Tab>("profile");
  const email = useAuthStore((s) => s.email);
  const agentId = useMetricsStore((s) => s.agentId);
  const connected = useMetricsStore((s) => s.connected);
  const logout = useAuthStore((s) => s.logout);

  return (
    <div className="settings-app">
      <div className="settings-sidebar">
        <button
          className={`settings-nav ${tab === "profile" ? "settings-nav-active" : ""}`}
          onClick={() => setTab("profile")}
        >
          Profile
        </button>
        <button
          className={`settings-nav ${tab === "agents" ? "settings-nav-active" : ""}`}
          onClick={() => setTab("agents")}
        >
          Agents
        </button>
        <button
          className={`settings-nav ${tab === "appearance" ? "settings-nav-active" : ""}`}
          onClick={() => setTab("appearance")}
        >
          Appearance
        </button>
      </div>

      <div className="settings-content">
        {tab === "profile" && (
          <div className="settings-section">
            <h3 className="settings-title">Profile</h3>
            <div className="settings-field">
              <label className="settings-label">Email</label>
              <span className="settings-value">{email ?? "—"}</span>
            </div>
            <div className="settings-field">
              <label className="settings-label">Authentication</label>
              <span className="settings-value">JWT + TOTP 2FA (configurable)</span>
            </div>
            <button className="settings-btn settings-btn-danger" onClick={logout}>
              Sign Out
            </button>
          </div>
        )}

        {tab === "agents" && (
          <div className="settings-section">
            <h3 className="settings-title">Connected Agents</h3>
            <div className="settings-agent-card">
              <div className="settings-agent-info">
                <span className="settings-agent-name">{agentId ?? "None"}</span>
                <span
                  className="settings-agent-status"
                  style={{ color: connected ? "var(--color-success)" : "var(--color-danger)" }}
                >
                  {connected ? "Connected" : "Disconnected"}
                </span>
              </div>
            </div>
            <div className="settings-hint">
              To add remote agents, install the GlassOps Agent on the target server
              and point GLASSOPS_SERVER_URL to this instance.
            </div>
          </div>
        )}

        {tab === "appearance" && (
          <div className="settings-section">
            <h3 className="settings-title">Appearance</h3>
            <div className="settings-field">
              <label className="settings-label">Theme</label>
              <span className="settings-value">Dark (default)</span>
            </div>
            <div className="settings-hint">
              Light mode and custom themes coming soon.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
