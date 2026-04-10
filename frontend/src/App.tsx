import { useEffect } from "react";
import { useAuthStore } from "./stores/authStore";
import Desktop from "./components/desktop/Desktop";
import LoginScreen from "./components/LoginScreen";
import PasswordChangeScreen from "./components/PasswordChangeScreen";

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const isBootstrapping = useAuthStore((s) => s.isBootstrapping);
  const mustChangePassword = useAuthStore((s) => s.mustChangePassword);
  const bootstrap = useAuthStore((s) => s.bootstrap);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  // Gate render only when we have nothing to show yet — avoids flashing LoginScreen
  // in a fresh tab whose httpOnly cookies still grant a valid session.
  if (isBootstrapping && !isAuthenticated) return null;
  if (!isAuthenticated) return <LoginScreen />;
  if (mustChangePassword) return <PasswordChangeScreen />;
  return <Desktop />;
}
