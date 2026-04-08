import { useCallback } from "react";
import { Rnd } from "react-rnd";
import type { WindowState } from "../../types";
import { useWindowStore } from "../../stores/windowStore";

interface WindowProps {
  window: WindowState;
  children: React.ReactNode;
}

const MENU_BAR_HEIGHT = 36;
const DOCK_HEIGHT = 72;

export default function Window({ window: win, children }: WindowProps) {
  const closeWindow = useWindowStore((s) => s.closeWindow);
  const minimizeWindow = useWindowStore((s) => s.minimizeWindow);
  const maximizeWindow = useWindowStore((s) => s.maximizeWindow);
  const restoreWindow = useWindowStore((s) => s.restoreWindow);
  const focusWindow = useWindowStore((s) => s.focusWindow);
  const updateWindowPosition = useWindowStore((s) => s.updateWindowPosition);
  const updateWindowSize = useWindowStore((s) => s.updateWindowSize);
  const updateWindowOpacity = useWindowStore((s) => s.updateWindowOpacity);

  const handleMaximizeToggle = useCallback(() => {
    if (win.isMaximized) {
      restoreWindow(win.id);
    } else {
      maximizeWindow(win.id, {
        x: win.x,
        y: win.y,
        width: win.width,
        height: win.height,
      });
    }
  }, [win, maximizeWindow, restoreWindow]);

  if (win.isMinimized) return null;

  const isMax = win.isMaximized;
  // window-manager is already positioned below menubar, so max position is 0,0
  const position = isMax ? { x: 0, y: 0 } : { x: win.x, y: win.y };
  const size = isMax
    ? {
        width: globalThis.innerWidth,
        height: globalThis.innerHeight - MENU_BAR_HEIGHT - DOCK_HEIGHT,
      }
    : { width: win.width, height: win.height };

  return (
    <Rnd
      position={position}
      size={size}
      minWidth={win.minWidth}
      minHeight={win.minHeight}
      disableDragging={isMax}
      enableResizing={!isMax}
      dragHandleClassName="window-titlebar"
      bounds="parent"
      style={{ zIndex: win.zIndex }}
      onDragStart={() => focusWindow(win.id)}
      onDragStop={(_e, d) => updateWindowPosition(win.id, d.x, d.y)}
      onResizeStop={(_e, _dir, ref, _delta, pos) => {
        updateWindowSize(win.id, ref.offsetWidth, ref.offsetHeight);
        updateWindowPosition(win.id, pos.x, pos.y);
      }}
      onMouseDown={() => focusWindow(win.id)}
    >
      <div className="window-container" style={{ opacity: win.opacity }}>
        {/* Title Bar */}
        <div
          className="window-titlebar"
          onDoubleClick={handleMaximizeToggle}
        >
          <div className="window-traffic-lights">
            <button
              className="traffic-light traffic-close"
              onClick={() => closeWindow(win.id)}
              aria-label="Close"
            >
              <svg width="6" height="6" viewBox="0 0 6 6">
                <path d="M0.5 0.5L5.5 5.5M5.5 0.5L0.5 5.5" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            </button>
            <button
              className="traffic-light traffic-minimize"
              onClick={() => minimizeWindow(win.id)}
              aria-label="Minimize"
            >
              <svg width="6" height="2" viewBox="0 0 6 2">
                <path d="M0.5 1H5.5" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            </button>
            <button
              className="traffic-light traffic-maximize"
              onClick={handleMaximizeToggle}
              aria-label="Maximize"
            >
              <svg width="6" height="6" viewBox="0 0 6 6">
                <rect x="0.5" y="0.5" width="5" height="5" stroke="currentColor" strokeWidth="1" fill="none" />
              </svg>
            </button>
          </div>
          <span className="window-title">{win.title}</span>
          <div
            className="window-opacity-control"
            onMouseDown={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <input
              type="range"
              min="30"
              max="100"
              value={Math.round(win.opacity * 100)}
              onChange={(e) =>
                updateWindowOpacity(win.id, Number(e.target.value) / 100)
              }
              className="window-opacity-slider"
              title={`Opacity: ${Math.round(win.opacity * 100)}%`}
            />
          </div>
        </div>

        {/* Content */}
        <div className="window-content">{children}</div>
      </div>
    </Rnd>
  );
}
