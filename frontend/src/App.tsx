import { useAuthStore } from "./stores/authStore";
import Desktop from "./components/desktop/Desktop";
import LoginScreen from "./components/LoginScreen";
import PasswordChangeScreen from "./components/PasswordChangeScreen";

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const mustChangePassword = useAuthStore((s) => s.mustChangePassword);

  if (!isAuthenticated) return <LoginScreen />;
  if (mustChangePassword) return <PasswordChangeScreen />;
  return <Desktop />;
}
