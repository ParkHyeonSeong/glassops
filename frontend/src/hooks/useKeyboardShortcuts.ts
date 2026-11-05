import { useEffect } from "react";
import { useWindowStore } from "../stores/windowStore";

export function useKeyboardShortcuts() {
  const closeFocusedWindow = useWindowStore((s) => s.closeFocusedWindow);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;

      // Cmd+W → always prevent, close focused window if any
      if (meta && e.key === "w") {
        e.preventDefault();
        e.stopPropagation();
        closeFocusedWindow();
        return;
      }

      // Esc → close focused window, unless it comes from the dock menu, which closes itself
      if (e.key === "Escape") {
        if (e.target instanceof Element && e.target.closest("[data-dock-menu]")) return;
        closeFocusedWindow();
      }
    };

    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, [closeFocusedWindow]);
}
