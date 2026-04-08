import { useAuthStore } from "./stores/authStore";
import Desktop from "./components/desktop/Desktop";
import LoginScreen from "./components/LoginScreen";

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  return isAuthenticated ? <Desktop /> : <LoginScreen />;
}
