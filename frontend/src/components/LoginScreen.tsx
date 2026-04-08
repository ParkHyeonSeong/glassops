import { useState } from "react";
import { Lock } from "lucide-react";
import { useAuthStore } from "../stores/authStore";
import { useServerTime } from "../hooks/useServerTime";

export default function LoginScreen() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState<"email" | "password">("email");
  const login = useAuthStore((s) => s.login);
  const time = useServerTime();

  const isValidEmail = (value: string) =>
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

  const handleEmailSubmit = () => {
    if (!isValidEmail(email)) {
      setError("Please enter a valid email address.");
      return;
    }
    setError("");
    setStep("password");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    const result = await login(email, password);
    if (!result.ok) {
      if (result.requiresTotp) {
        setError("2FA required — enter TOTP code (not yet implemented in UI)");
      } else {
        setError(result.error || "Incorrect password. Try again.");
      }
      setPassword("");
    }
    setLoading(false);
  };

  const handleBack = () => {
    setStep("email");
    setPassword("");
    setError("");
  };

  const dateStr = time.toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
  });

  const timeStr = time.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  // Extract display name from email (part before @), with fallback
  const displayName =
    (email.includes("@") ? email.split("@")[0] : email) || "User";

  return (
    <div className="login-screen">
      <div className="login-bg" />

      {/* Clock — fixed to top area */}
      <div className="login-clock">
        <div className="login-clock-time">{timeStr}</div>
        <div className="login-clock-date">{dateStr}</div>
      </div>

      {/* Spacer to push profile below center */}
      <div className="login-spacer" />

      {/* User profile */}
      <div className="login-center">
        {step === "email" ? (
          <>
            <div className="login-avatar">
              <Lock size={28} strokeWidth={1.5} />
            </div>
            <form
              className="login-pill-form"
              onSubmit={(e) => {
                e.preventDefault();
                handleEmailSubmit();
              }}
            >
              <div className="login-pill">
                <input
                  type="email"
                  placeholder="Email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="login-pill-input"
                  autoFocus
                />
                <button
                  type="submit"
                  className="login-pill-btn"
                  disabled={!isValidEmail(email)}
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
              {error && <p className="login-error">{error}</p>}
            </form>
          </>
        ) : (
          <>
            <div className="login-avatar login-avatar-active">
              <span className="login-avatar-initial">
                {displayName.charAt(0).toUpperCase()}
              </span>
            </div>
            <button className="login-display-name" onClick={handleBack}>
              {displayName}
            </button>
            <form className="login-pill-form" onSubmit={handleSubmit}>
              <div className="login-pill">
                <input
                  type="password"
                  placeholder="Password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="login-pill-input"
                  autoFocus
                />
                <button
                  type="submit"
                  className="login-pill-btn"
                  disabled={loading || !password}
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
              {error && <p className="login-error">{error}</p>}
            </form>
          </>
        )}
      </div>

      {/* Bottom branding */}
      <div className="login-bottom">
        <span className="login-brand">GlassOps</span>
      </div>
    </div>
  );
}
