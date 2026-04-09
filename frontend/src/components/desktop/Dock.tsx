import { useState, useCallback } from "react";
import {
  BarChart3,
  Box,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
} from "lucide-react";
import { APP_DEFINITIONS, useWindowStore } from "../../stores/windowStore";

const ICON_MAP: Record<string, React.ComponentType<{ size?: number }>> = {
  BarChart3,
  Container,
  Globe,
  ListTree,
  FileText,
  TerminalSquare,
  Settings,
};

const FallbackIcon = Box;

export default function Dock() {
  const windows = useWindowStore((s) => s.windows);
  const openWindow = useWindowStore((s) => s.openWindow);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [dockVisible, setDockVisible] = useState(true);

  const hasMaximized = windows.some((w) => w.isMaximized);

  const getScale = useCallback(
    (index: number) => {
      if (hoveredIndex === null) return 1;
      const distance = Math.abs(index - hoveredIndex);
      if (distance === 0) return 1.4;
      if (distance === 1) return 1.2;
      if (distance === 2) return 1.08;
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
          {APP_DEFINITIONS.map((app, index) => {
            const Icon = ICON_MAP[app.icon] ?? FallbackIcon;
            const scale = getScale(index);
            const open = isAppOpen(app.id);
            const lift = (scale - 1) * 28;
            const isHovered = hoveredIndex === index;

            return (
              <div key={app.id} className="dock-item-col">
                {isHovered && (
                  <div className="dock-tooltip">{app.title}</div>
                )}
                <div
                  className="dock-item-lift"
                  style={{ transform: `translateY(-${lift}px)` }}
                >
                  <button
                    className="dock-item"
                    style={{ transform: `scale(${scale})` }}
                    onClick={() => openWindow(app.id)}
                    onMouseEnter={() => setHoveredIndex(index)}
                  >
                    <Icon size={30} />
                  </button>
                </div>
                <div className="dock-indicator-slot">
                  {open && <div className="dock-indicator" />}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
