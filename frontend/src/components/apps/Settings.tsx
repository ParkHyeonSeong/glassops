import { useState, useEffect } from "react";
import { useMetricsStore } from "../../stores/metricsStore";
import { useAuthStore } from "../../stores/authStore";
import { useSettingsStore, WALLPAPERS } from "../../stores/settingsStore";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { type AlertMetricKey } from "../../lib/thresholds";
import { formatApiDetail } from "../../lib/apiDetail";
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

type SecurityMode = "starttls" | "implicit_tls" | "none";

// What the API returns in place of a stored password, and what it accepts back to
// mean "keep the stored one" (alert_service.MASKED_PASSWORD).
const MASKED_PASSWORD = "********";

const SECURITY_OPTIONS: { value: SecurityMode; label: string; port: number }[] = [
  { value: "starttls", label: "STARTTLS", port: 587 },
  { value: "implicit_tls", label: "Implicit TLS", port: 465 },
  { value: "none", label: "None (no encryption)", port: 25 },
];

interface EmailThresholds {
  cpu_crit: number;
  mem_crit: number;
  disk_crit: number;
}

// The editable form shape — a projection of the GET response that carries ONLY the
// fields the POST body accepts. The GET response also returns configured,
// password_decrypt_failed and security_ambiguous, which SmtpConfig rejects
// (extra="forbid"); keeping them out of this type keeps them out of the payload.
interface EmailConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  from_email: string;
  to_email: string;
  // null = an ambiguous legacy row (both TLS flags set). The operator must pick
  // before anything can be saved; defaulting here would silently rewrite their
  // transport, which is the bug the backend canonicalisation exists to stop.
  security: SecurityMode | null;
  thresholds: EmailThresholds;
}

type EmailFieldKey = "host" | "port" | "username" | "password" | "from_email" | "to_email";

interface EmailField {
  key: EmailFieldKey;
  label: string;
  type?: "text" | "number" | "password";
  hint?: string;
}

const EMAIL_FIELDS: EmailField[] = [
  { key: "host", label: "SMTP Host", hint: "Hostname only — no smtp:// scheme, no port suffix." },
  { key: "port", label: "Port", type: "number" },
  { key: "username", label: "Username", hint: "SMTP login identifier. Leave blank for an unauthenticated relay." },
  { key: "password", label: "Password", type: "password" },
  { key: "from_email", label: "From Email", hint: "Required, unless the username is itself an email address." },
  { key: "to_email", label: "To Email (alerts)" },
];

const THRESHOLD_FIELDS: { key: keyof EmailThresholds; label: string }[] = [
  { key: "cpu_crit", label: "CPU critical (%)" },
  { key: "mem_crit", label: "Memory critical (%)" },
  { key: "disk_crit", label: "Disk critical (%)" },
];

function projectThresholds(raw: unknown): EmailThresholds {
  const src = (raw ?? {}) as Partial<Record<keyof EmailThresholds, unknown>>;
  const pick = (key: keyof EmailThresholds): number => {
    const v = Number(src[key]);
    return Number.isFinite(v) ? v : EMPTY_EMAIL_CONFIG.thresholds[key];
  };
  return { cpu_crit: pick("cpu_crit"), mem_crit: pick("mem_crit"), disk_crit: pick("disk_crit") };
}

const EMPTY_EMAIL_CONFIG: EmailConfig = {
  host: "", port: 587, username: "", password: "", from_email: "", to_email: "",
  security: "starttls",   // a NEW config legitimately defaults; a loaded null does not
  thresholds: { cpu_crit: 90, mem_crit: 90, disk_crit: 95 },
};

type EmailStatus =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "sending" }
  | { kind: "accepted"; detail: string }
  | { kind: "savedTestFailed"; detail: string }
  // The save landed but the /test outcome is genuinely unknown: the request may
  // have reached the relay and only the response was lost. Reporting that as a
  // failure invites a retry that duplicates the test mail.
  | { kind: "savedTestUnknown"; detail: string }
  | { kind: "error"; detail: string };

function EmailTab() {
  const role = useAuthStore((s) => s.role);
  const [config, setConfig] = useState<EmailConfig>(EMPTY_EMAIL_CONFIG);
  const [status, setStatus] = useState<EmailStatus>({ kind: "idle" });
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [decryptFailed, setDecryptFailed] = useState(false);
  const isAdmin = role === "admin";

  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    fetchWithAuth("/api/alerts/config")
      .then((r) => {
        // A 403/500 body is not a config. Falling through would render a blank
        // editable form, and saving it would overwrite the real stored settings.
        if (!r.ok) throw new Error(`config load failed: ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (cancelled) return;
        if (d.configured) {
          // Project the response into the form DTO explicitly. Only these fields
          // exist on EmailConfig, so the response-only fields (configured,
          // password_decrypt_failed, security_ambiguous) can never reach the POST.
          setConfig({
            host: d.host ?? "",
            port: Number(d.port ?? 587),
            username: d.username ?? "",
            password: d.password ?? "",
            from_email: d.from_email ?? "",
            to_email: d.to_email ?? "",
            // NEVER default an ambiguous legacy row (backend sends security: null
            // with security_ambiguous: true). Guessing here would re-introduce the
            // silent STARTTLS rewrite the backend fix exists to prevent.
            security: (d.security as SecurityMode | null) ?? null,
            // Field-wise, not a spread: `thresholds` was an untyped dict before the
            // strict schema, so a stored row can still carry arbitrary keys. Spreading
            // would carry them back into the POST, where extra="forbid" 422s — and the
            // UI offers no way to remove them.
            thresholds: projectThresholds(d.thresholds),
          });
          setDecryptFailed(Boolean(d.password_decrypt_failed));
        }
        setLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setLoadError(true);
        setLoaded(true);
      });
    return () => { cancelled = true; };
  }, [isAdmin]);

  const updateField = (key: EmailFieldKey, rawValue: string) => {
    setStatus({ kind: "idle" });
    setConfig((prev) => (
      key === "port" ? { ...prev, port: Number(rawValue) } : { ...prev, [key]: rawValue }
    ));
  };

  const updateThreshold = (key: keyof EmailThresholds, rawValue: string) => {
    setStatus({ kind: "idle" });
    setConfig((prev) => ({
      ...prev, thresholds: { ...prev.thresholds, [key]: Number(rawValue) },
    }));
  };

  // Save first, then test the configuration that was just persisted — testing a
  // dirty form against the last-saved config silently checks the wrong settings.
  const handleSaveAndTest = async () => {
    // Snapshot the payload so a late edit cannot change what gets tested. `config`
    // holds only the DTO fields, and the button is disabled while security is null,
    // so `security` is a concrete mode here.
    const payload = JSON.stringify({ ...config, clear_password: false });
    setStatus({ kind: "saving" });
    try {
      const saveRes = await fetchWithAuth("/api/alerts/config", { method: "POST", body: payload });
      if (!saveRes.ok) {
        const d = await saveRes.json().catch(() => ({}));
        setStatus({ kind: "error", detail: formatApiDetail(d.detail, "Save failed") });
        return;
      }
      // The credential is stored now, so stop holding the plaintext in the DOM and
      // drop any stale decrypt warning — the row it referred to has been replaced.
      setConfig((prev) => ({ ...prev, password: MASKED_PASSWORD }));
      setDecryptFailed(false);
    } catch (e) {
      // Save-phase failure only. The config was NOT stored.
      setStatus({ kind: "error", detail: e instanceof Error ? e.message : "Save failed" });
      return;
    }

    // Separate try: past this point the save HAS landed, so every failure — including
    // a thrown network error — must still tell the operator their settings are stored.
    setStatus({ kind: "sending" });
    try {
      const testRes = await fetchWithAuth("/api/alerts/test", { method: "POST" });
      const d = await testRes.json().catch(() => ({}));
      if (!testRes.ok) {
        setStatus({ kind: "savedTestFailed", detail: formatApiDetail(d.detail, "Send failed") });
        return;
      }
      setStatus({ kind: "accepted",
                  detail: formatApiDetail(d.detail, "SMTP server accepted the message") });
    } catch (e) {
      // No response at all — outcome unknown, not failed.
      setStatus({ kind: "savedTestUnknown",
                  detail: e instanceof Error ? e.message : "Request failed" });
    }
  };

  if (!isAdmin) {
    return (
      <div className="settings-section">
        <h3 className="settings-title">Email Alerts (SMTP)</h3>
        <p className="settings-hint">Admin access required to view or change SMTP settings.</p>
      </div>
    );
  }
  if (!loaded) return <p className="settings-hint">Loading...</p>;
  if (loadError) {
    // Error only — never a blank editable form, which an operator could "fix" by
    // saving, overwriting the real stored configuration with empty values.
    return (
      <div className="settings-section">
        <h3 className="settings-title">Email Alerts (SMTP)</h3>
        <p className="settings-msg">
          Could not load the email settings. Check the connection and reopen this tab.
        </p>
      </div>
    );
  }

  const pending = status.kind === "saving" || status.kind === "sending";
  // Only rendered once a mode is chosen, so the lookup always hits.
  const recommendedPort = SECURITY_OPTIONS.find((o) => o.value === config.security)?.port;

  return (
    <div className="settings-section">
      <h3 className="settings-title">Email Alerts (SMTP)</h3>
      {decryptFailed && (
        <p className="settings-msg">
          The stored password could not be decrypted (the master secret changed).
          Re-enter it before saving.
        </p>
      )}

      {EMAIL_FIELDS.map((f) => (
        <div key={f.key} className="settings-field">
          <label className="settings-label" htmlFor={`email-${f.key}`}>{f.label}</label>
          <input
            id={`email-${f.key}`}
            type={f.type || "text"}
            value={config[f.key]}
            onChange={(event) => updateField(f.key, event.target.value)}
            disabled={pending}
            className="settings-input"
          />
          {f.hint && <span className="settings-hint">{f.hint}</span>}
        </div>
      ))}

      <div className="settings-field">
        <label className="settings-label" htmlFor="email-security">Security</label>
        <select
          id="email-security"
          className="settings-input"
          value={config.security ?? ""}
          disabled={pending}
          onChange={(e) => {
            setStatus({ kind: "idle" });
            setConfig((prev) => ({ ...prev, security: e.target.value as SecurityMode }));
          }}
        >
          {config.security === null && (
            <option value="" disabled>— select a security mode —</option>
          )}
          {SECURITY_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {config.security === null ? (
          // security=null means the stored transport could not be determined — an
          // unsupported mode, a non-boolean flag, or contradictory flags. Naming one
          // cause would be a guess, and no port can be recommended without a mode.
          <span className="settings-msg">
            The stored TLS configuration could not be determined. Pick a mode before
            saving — alerts will not send until you do.
          </span>
        ) : (
          <span className="settings-hint">
            Recommended port: {recommendedPort}. Allowed ports are 25, 465, 587 and 2525.
          </span>
        )}
      </div>

      <h4 className="settings-subtitle">Email critical thresholds</h4>
      <p className="settings-hint">
        Server-side, and separate from the in-browser thresholds under Settings &gt; Alerts.
        These decide when an email is sent, even with nobody logged in.
      </p>
      {THRESHOLD_FIELDS.map((t) => (
        <div key={t.key} className="settings-field">
          <label className="settings-label" htmlFor={`email-${t.key}`}>{t.label}</label>
          <input
            id={`email-${t.key}`}
            type="number"
            min={0}
            max={100}
            value={config.thresholds[t.key]}
            onChange={(e) => updateThreshold(t.key, e.target.value)}
            disabled={pending}
            className="settings-input"
            style={{ width: 80 }}
          />
        </div>
      ))}

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button className="settings-btn" onClick={handleSaveAndTest}
          disabled={pending || !config.host || config.security === null}>
          {status.kind === "saving" ? "Saving..."
            : status.kind === "sending" ? "Sending..."
            : "Save & Send Test"}
        </button>
      </div>

      {status.kind === "accepted" && (
        <p className="settings-msg">{status.detail} — check the inbox to confirm delivery.</p>
      )}
      {status.kind === "savedTestFailed" && (
        <p className="settings-msg">Settings saved, but the test send failed: {status.detail}</p>
      )}
      {status.kind === "savedTestUnknown" && (
        <p className="settings-msg">
          Settings saved, but we could not confirm the test result: {status.detail}.
          The message may still have been sent — check the inbox before retrying.
        </p>
      )}
      {status.kind === "error" && <p className="settings-msg">{status.detail}</p>}
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
