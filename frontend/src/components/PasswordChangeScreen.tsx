import { useState, useMemo, useCallback } from "react";
import { Check, X, Copy, RefreshCw } from "lucide-react";
import { useAuthStore } from "../stores/authStore";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";

const SPECIAL_CHARS = "!@#$%^&*()_+-=[]{}|;:,.<>?";

function generatePassword(length = 20): string {
  const upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const lower = "abcdefghijklmnopqrstuvwxyz";
  const digits = "0123456789";
  const all = upper + lower + digits + SPECIAL_CHARS;

  const array = new Uint32Array(length);
  crypto.getRandomValues(array);

  // Ensure at least one of each type
  const chars = [
    upper[array[0] % upper.length],
    lower[array[1] % lower.length],
    digits[array[2] % digits.length],
    SPECIAL_CHARS[array[3] % SPECIAL_CHARS.length],
  ];

  for (let i = 4; i < length; i++) {
    chars.push(all[array[i] % all.length]);
  }

  // Shuffle
  for (let i = chars.length - 1; i > 0; i--) {
    const j = array[i] % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }

  return chars.join("");
}

function PolicyCheck({ ok, label }: { ok: boolean; label: string }) {
  return (
    <div className="pw-policy-check">
      {ok ? (
        <Check size={13} className="pw-check-ok" />
      ) : (
        <X size={13} className="pw-check-fail" />
      )}
      <span className={ok ? "pw-check-ok" : "pw-check-fail"}>{label}</span>
    </div>
  );
}

export default function PasswordChangeScreen() {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const accessToken = useAuthStore((s) => s.accessToken);
  const setMustChange = useAuthStore((s) => s.clearMustChangePassword);

  const checks = useMemo(() => ({
    length: password.length >= 8 && password.length <= 256,
    uppercase: /[A-Z]/.test(password),
    lowercase: /[a-z]/.test(password),
    digit: /[0-9]/.test(password),
    special: /[^A-Za-z0-9]/.test(password),
    match: password.length > 0 && password === confirm,
  }), [password, confirm]);

  const allValid = Object.values(checks).every(Boolean);

  const handleGenerate = useCallback(() => {
    const pw = generatePassword(24);
    setPassword(pw);
    setConfirm(pw);
    setCopied(false);
  }, []);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(password);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [password]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!allValid) return;

    setLoading(true);
    setError("");

    try {
      const res = await fetch(`${BACKEND_URL}/api/auth/force-password`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ new_password: password }),
      });

      if (res.ok) {
        setMustChange();
      } else {
        const data = await res.json().catch(() => ({}));
        setError(data.detail?.error || data.detail || "Failed to change password");
      }
    } catch {
      setError("Cannot connect to server");
    }
    setLoading(false);
  };

  return (
    <div className="login-screen">
      <div className="login-bg" />

      <div className="pw-change-card">
        <h2 className="pw-change-title">Change Your Password</h2>
        <p className="pw-change-desc">
          You're using the default password. Please set a secure password to continue.
        </p>

        <form onSubmit={handleSubmit} className="pw-change-form">
          <div className="pw-input-group">
            <input
              type="password"
              placeholder="New password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="pw-input"
              autoFocus
            />
            <input
              type="password"
              placeholder="Confirm password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="pw-input"
            />
          </div>

          {/* Policy checks */}
          <div className="pw-policy">
            <PolicyCheck ok={checks.length} label="8–256 characters" />
            <PolicyCheck ok={checks.uppercase} label="Uppercase letter" />
            <PolicyCheck ok={checks.lowercase} label="Lowercase letter" />
            <PolicyCheck ok={checks.digit} label="Number" />
            <PolicyCheck ok={checks.special} label="Special character" />
            <PolicyCheck ok={checks.match} label="Passwords match" />
          </div>

          {/* Generate + Copy */}
          <div className="pw-actions">
            <button type="button" className="pw-gen-btn" onClick={handleGenerate}>
              <RefreshCw size={13} /> Generate Strong Password
            </button>
            {password && (
              <button type="button" className="pw-copy-btn" onClick={handleCopy}>
                <Copy size={13} /> {copied ? "Copied!" : "Copy"}
              </button>
            )}
          </div>

          {error && <p className="pw-error">{error}</p>}

          <button type="submit" className="pw-submit" disabled={!allValid || loading}>
            {loading ? "Saving..." : "Set Password & Continue"}
          </button>
        </form>
      </div>
    </div>
  );
}
