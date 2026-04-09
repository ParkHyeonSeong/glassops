import { useEffect, useRef } from "react";
import { useAuthStore } from "./stores/authStore";
import Desktop from "./components/desktop/Desktop";
import LoginScreen from "./components/LoginScreen";
import PasswordChangeScreen from "./components/PasswordChangeScreen";

function useSessionValidation() {
  const validatedRef = useRef(false);

  useEffect(() => {
    if (validatedRef.current) return;
    validatedRef.current = true;

    const { isAuthenticated, accessToken, refresh, logout } = useAuthStore.getState();
    if (!isAuthenticated || !accessToken) return;

    const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";
    fetch(`${BACKEND_URL}/api/auth/me`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      credentials: "include",
    }).then((res) => {
      if (res.status === 401) {
        refresh().then((ok) => {
          if (!ok) logout();
        });
      }
    }).catch(() => {});
  }, []);
}

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const mustChangePassword = useAuthStore((s) => s.mustChangePassword);

  useSessionValidation();

  if (!isAuthenticated) return <LoginScreen />;
  if (mustChangePassword) return <PasswordChangeScreen />;
  return <Desktop />;
}
