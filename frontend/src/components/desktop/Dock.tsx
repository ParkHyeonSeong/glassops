import { useState, useCallback } from "react";
import {
  BarChart3,
  Box,
  Cpu,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
  Users,
} from "lucide-react";
import { APP_DEFINITIONS, useWindowStore } from "../../stores/windowStore";
import { useAuthStore } from "../../stores/authStore";

const ICON_MAP: Record<string, React.ComponentType<{ size?: number }>> = {
  BarChart3,
  Cpu,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
  Users,
};

const FallbackIcon = Box;

export default function Dock() {
  const windows = useWindowStore((s) => s.windows);
  const openWindow = useWindowStore((s) => s.openWindow);
  const role = useAuthStore((s) => s.role);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [dockVisible, setDockVisible] = useState(true);

  const visibleApps = APP_DEFINITIONS.filter(
    (app) => !app.hiddenFromLauncher && (!app.adminOnly || role === "admin"),
  );

  const hasMaximized = windows.some((w) => w.isMaximized);

  const getScale = useCallback(
    (index: number) => {
      if (hoveredIndex === null) return 1;
      const distance = Math.abs(index - hoveredIndex);
      if (distance === 0) return 1.32;
      if (distance === 1) return 1.16;
      if (distance === 2) return 1.06;
      return 1;
    },
    [hoveredIndex]
  );

  const isAppOpen = (appId: string) =>
    windows.some((w) => w.appId === appId);

  // Auto-hide when a window is maximized
  const shouldHide = hasMaximized && !dockVisible;

  return (
    <>
      {/* Invisible hover zone at bottom to trigger dock */}
      {hasMaximized && (
        <div
          className="dock-trigger-zone"
          onMouseEnter={() => setDockVisible(true)}
        />
      )}

      <div
        className={`dock-wrapper ${shouldHide ? "dock-hidden" : ""}`}
        onMouseLeave={() => {
          setHoveredIndex(null);
          if (hasMaximized) setDockVisible(false);
        }}
      >
        <div className="dock">
          {visibleApps.map((app, index) => {
            const Icon = ICON_MAP[app.icon] ?? FallbackIcon;
            const scale = getScale(index);
            const open = isAppOpen(app.id);
            const lift = (scale - 1) * 24;
            const isHovered = hoveredIndex === index;

            return (
              <div key={app.id} className="dock-item-col">
                {isHovered && (
                  <div className="dock-tooltip">
                    <span className="dock-tooltip-label">{app.title}</span>
                    <span className="dock-tooltip-caret" aria-hidden />
                  </div>
                )}
                <div
                  className="dock-item-lift"
                  style={{ transform: `translateY(-${lift}px)` }}
                >
                  <button
                    className={`dock-item${open ? " dock-item--open" : ""}`}
                    style={{ transform: `scale(${scale})` }}
                    onClick={() => openWindow(app.id)}
                    onMouseEnter={() => setHoveredIndex(index)}
                    aria-label={open ? `${app.title} (open)` : app.title}
                  >
                    <Icon size={28} />
                    <span className="dock-item-seam" aria-hidden />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
