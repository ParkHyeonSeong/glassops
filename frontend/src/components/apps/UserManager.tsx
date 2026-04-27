import { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, Trash2, KeyRound, ShieldCheck, X } from "lucide-react";
import { fetchWithAuth } from "../../utils/api";
import { useAuthStore } from "../../stores/authStore";
import { useMetricsStore } from "../../stores/metricsStore";

interface UserRow {
  email: string;
  role: "admin" | "user";
  is_active: boolean;
  totp_enabled: boolean;
  must_change_password: boolean;
  created_at: number;
}

type DialogMode = null | "create" | { mode: "edit"; email: string };

export default function UserManager() {
  const me = useAuthStore((s) => s.email);
  const knownAgentIds = useMetricsStore((s) => s.agentIds);

  const [users, setUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogMode>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchWithAuth("/api/users");
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || `Failed to load (${res.status})`);
        setUsers([]);
        return;
      }
      const data = await res.json();
      setUsers(data.users || []);
    } catch {
      setError("Failed to connect to backend");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const editingEmail = typeof dialog === "object" && dialog ? dialog.email : null;

  return (
    <div className="user-manager">
      <div className="user-manager-header">
        <h2 className="user-manager-title">Users</h2>
        <button className="user-manager-add" onClick={() => setDialog("create")}>
          <Plus size={14} /> New user
        </button>
      </div>

      {error && <div className="user-manager-error" onClick={() => setError(null)}>{error}</div>}

      <div className="user-manager-table-wrap">
        <table className="user-manager-table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>2FA</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={6} className="user-manager-empty">Loading…</td></tr>
            )}
            {!loading && users.length === 0 && (
              <tr><td colSpan={6} className="user-manager-empty">No users.</td></tr>
            )}
            {users.map((u) => (
              <tr key={u.email} className={u.is_active ? "" : "user-row-disabled"}>
                <td>
                  <span className="user-cell-email">{u.email}</span>
                  {u.email === me && <span className="user-badge-self">you</span>}
                </td>
                <td>{u.role === "admin" ? <span className="user-badge-admin"><ShieldCheck size={11} /> admin</span> : "user"}</td>
                <td>{u.is_active ? <span className="user-status-active">active</span> : <span className="user-status-inactive">disabled</span>}</td>
                <td>{u.totp_enabled ? "✓" : "—"}</td>
                <td>{new Date(u.created_at * 1000).toLocaleDateString()}</td>
                <td className="user-cell-actions">
                  <button className="user-action-btn" onClick={() => setDialog({ mode: "edit", email: u.email })}>Edit</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {dialog === "create" && (
        <CreateUserDialog
          onClose={() => setDialog(null)}
          onCreated={() => { setDialog(null); refresh(); }}
        />
      )}

      {editingEmail && (
        <EditUserDialog
          email={editingEmail}
          isSelf={editingEmail === me}
          knownAgentIds={knownAgentIds}
          onClose={() => setDialog(null)}
          onChanged={() => { setDialog(null); refresh(); }}
        />
      )}
    </div>
  );
}

function formatError(detail: unknown): string | null {
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object") {
    const d = detail as { error?: string; checks?: Record<string, boolean> };
    if (d.error && d.checks) {
      const failed = Object.entries(d.checks).filter(([, ok]) => !ok).map(([k]) => k);
      return failed.length ? `${d.error} (missing: ${failed.join(", ")})` : d.error;
    }
    if (d.error) return d.error;
    try { return JSON.stringify(detail); } catch { return null; }
  }
  return String(detail);
}

function CreateUserDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetchWithAuth("/api/users", {
        method: "POST",
        body: JSON.stringify({ email, password, role }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(formatError(data.detail) || `Request failed (${res.status})`);
        return;
      }
      onCreated();
    } catch {
      setError("Failed to connect to backend");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="Create user" onClose={onClose}>
      <label className="user-form-label">Email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} className="user-form-input" autoFocus />
      </label>
      <label className="user-form-label">Initial password
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="user-form-input" />
      </label>
      <p className="user-form-hint">Must include upper, lower, digit, special. The user will be required to change it on first login.</p>
      <label className="user-form-label">Role
        <select value={role} onChange={(e) => setRole(e.target.value as "user" | "admin")} className="user-form-input">
          <option value="user">User</option>
          <option value="admin">Admin</option>
        </select>
      </label>
      {error && <div className="user-form-error">{error}</div>}
      <div className="user-form-actions">
        <button className="user-action-btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button className="user-action-btn user-action-primary" onClick={submit} disabled={submitting || !email || !password}>
          {submitting ? "Creating…" : "Create"}
        </button>
      </div>
    </Dialog>
  );
}

function EditUserDialog({ email, isSelf, knownAgentIds, onClose, onChanged }: {
  email: string;
  isSelf: boolean;
  knownAgentIds: string[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [role, setRole] = useState<"user" | "admin">("user");
  const [isActive, setIsActive] = useState(true);
  const [newPassword, setNewPassword] = useState("");
  const [hostMap, setHostMap] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Always include all known agents in the editor; pre-fill from server.
  const agentRows = useMemo(() => {
    const set = new Set<string>([...knownAgentIds, ...Object.keys(hostMap)]);
    return Array.from(set).sort();
  }, [knownAgentIds, hostMap]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const [usersRes, hostsRes] = await Promise.all([
          fetchWithAuth("/api/users"),
          fetchWithAuth(`/api/users/${encodeURIComponent(email)}/hosts`),
        ]);
        if (cancelled) return;
        if (usersRes.ok) {
          const data = await usersRes.json();
          const u = (data.users || []).find((x: UserRow) => x.email === email);
          if (u) {
            setRole(u.role);
            setIsActive(u.is_active);
          }
        }
        if (hostsRes.ok) {
          const data = await hostsRes.json();
          setHostMap(data.accounts || {});
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [email]);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const userPayload: Record<string, unknown> = { role, is_active: isActive };
      if (newPassword) userPayload.new_password = newPassword;
      const userRes = await fetchWithAuth(`/api/users/${encodeURIComponent(email)}`, {
        method: "PATCH",
        body: JSON.stringify(userPayload),
      });
      if (!userRes.ok) {
        const data = await userRes.json().catch(() => ({}));
        setError(formatError(data.detail) || `Update failed (${userRes.status})`);
        return;
      }
      const hostRes = await fetchWithAuth(`/api/users/${encodeURIComponent(email)}/hosts`, {
        method: "PUT",
        body: JSON.stringify({ accounts: hostMap }),
      });
      if (!hostRes.ok) {
        const data = await hostRes.json().catch(() => ({}));
        setError(formatError(data.detail) || `Save failed (${hostRes.status})`);
        return;
      }
      onChanged();
    } catch {
      setError("Failed to connect to backend");
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async () => {
    if (!confirm(`Delete ${email}?`)) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetchWithAuth(`/api/users/${encodeURIComponent(email)}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(formatError(data.detail) || `Delete failed (${res.status})`);
        return;
      }
      onChanged();
    } catch {
      setError("Failed to connect to backend");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title={`Edit ${email}`} onClose={onClose}>
      {loading ? (
        <div className="user-form-hint">Loading…</div>
      ) : (
        <>
          <label className="user-form-label">Role
            <select value={role} onChange={(e) => setRole(e.target.value as "user" | "admin")} className="user-form-input" disabled={isSelf}>
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label className="user-form-label user-form-label-row">
            <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} disabled={isSelf} />
            Active
          </label>
          <label className="user-form-label">
            <span className="user-form-label-text"><KeyRound size={11} /> Reset password (optional)</span>
            <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className="user-form-input" placeholder="Leave blank to keep current" />
          </label>

          <div className="user-form-section">
            <h4 className="user-form-section-title">Host shell accounts</h4>
            <p className="user-form-hint">Username this account uses when opening a terminal on each host. Leave empty to deny terminal access on that host.</p>
            {agentRows.length === 0 && (
              <p className="user-form-hint">No agents are currently connected. Connect an agent and re-open this dialog.</p>
            )}
            {agentRows.map((agentId) => (
              <div key={agentId} className="user-host-row">
                <span className="user-host-agent">{agentId}</span>
                <input
                  type="text"
                  value={hostMap[agentId] ?? ""}
                  onChange={(e) => setHostMap((prev) => ({ ...prev, [agentId]: e.target.value }))}
                  placeholder="(no access)"
                  className="user-form-input user-host-input"
                />
              </div>
            ))}
          </div>

          {error && <div className="user-form-error">{error}</div>}
          <div className="user-form-actions">
            {!isSelf && (
              <button className="user-action-btn user-action-danger" onClick={remove} disabled={submitting}>
                <Trash2 size={11} /> Delete
              </button>
            )}
            <div className="user-form-actions-right">
              <button className="user-action-btn" onClick={onClose} disabled={submitting}>Cancel</button>
              <button className="user-action-btn user-action-primary" onClick={submit} disabled={submitting}>
                {submitting ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </>
      )}
    </Dialog>
  );
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="user-dialog-backdrop" onClick={onClose}>
      <div className="user-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="user-dialog-header">
          <h3 className="user-dialog-title">{title}</h3>
          <button className="user-dialog-close" onClick={onClose} aria-label="Close"><X size={14} /></button>
        </div>
        <div className="user-dialog-body">{children}</div>
      </div>
    </div>
  );
}

