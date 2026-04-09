import { useCallback, useState } from "react";
import { Rnd } from "react-rnd";
import type { WindowState } from "../../types";
import { useWindowStore } from "../../stores/windowStore";

interface WindowProps {
  window: WindowState;
  children: React.ReactNode;
}

const MENU_BAR_HEIGHT = 36;
const SNAP_EDGE = 8;

type SnapZone = "left" | "right" | null;

function detectSnapZone(x: number, _y: number): SnapZone {
  const screenW = globalThis.innerWidth;
  if (x <= SNAP_EDGE) return "left";
  if (x >= screenW - SNAP_EDGE) return "right";
  return null;
}

export default function Window({ window: win, children }: WindowProps) {
  const closeWindow = useWindowStore((s) => s.closeWindow);
  const minimizeWindow = useWindowStore((s) => s.minimizeWindow);
  const maximizeWindow = useWindowStore((s) => s.maximizeWindow);
  const restoreWindow = useWindowStore((s) => s.restoreWindow);
  const focusWindow = useWindowStore((s) => s.focusWindow);
  const updateWindowPosition = useWindowStore((s) => s.updateWindowPosition);
  const updateWindowSize = useWindowStore((s) => s.updateWindowSize);
  const updateWindowOpacity = useWindowStore((s) => s.updateWindowOpacity);
  const snapWindow = useWindowStore((s) => s.snapWindow);

  const [snapPreview, setSnapPreview] = useState<SnapZone>(null);

  const handleMaximizeToggle = useCallback(() => {
    if (win.isMaximized) {
      restoreWindow(win.id);
    } else {
      maximizeWindow(win.id, {
        x: win.x, y: win.y, width: win.width, height: win.height,
      });
    }
  }, [win, maximizeWindow, restoreWindow]);

  if (win.isMinimized) return null;

  const isMax = win.isMaximized;
  const position = isMax ? { x: 0, y: 0 } : { x: win.x, y: win.y };
  // Maximized: cover full area including dock (dock auto-hides)
  const size = isMax
    ? { width: globalThis.innerWidth, height: globalThis.innerHeight - MENU_BAR_HEIGHT }
    : { width: win.width, height: win.height };

  return (
    <>
      {/* Snap preview overlay */}
      {snapPreview && (
        <div className={`snap-preview snap-preview-${snapPreview}`} />
      )}

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
        onDrag={(_e, d) => {
          setSnapPreview(detectSnapZone(d.x, d.y));
        }}
        onDragStop={(_e, d) => {
          const zone = detectSnapZone(d.x, d.y);
          setSnapPreview(null);
          if (zone === "left" || zone === "right") {
            snapWindow(win.id, zone);
          } else {
            updateWindowPosition(win.id, d.x, d.y);
          }
        }}
        onResizeStop={(_e, _dir, ref, _delta, pos) => {
          updateWindowSize(win.id, ref.offsetWidth, ref.offsetHeight);
          updateWindowPosition(win.id, pos.x, pos.y);
        }}
        onMouseDown={() => focusWindow(win.id)}
      >
        <div className="window-container" style={{ opacity: win.opacity }}>
          <div className="window-titlebar" onDoubleClick={handleMaximizeToggle}>
            <div className="window-traffic-lights">
              <button className="traffic-light traffic-close"
                onClick={() => closeWindow(win.id)} aria-label="Close">
                <svg width="6" height="6" viewBox="0 0 6 6">
                  <path d="M0.5 0.5L5.5 5.5M5.5 0.5L0.5 5.5" stroke="currentColor" strokeWidth="1.2" />
                </svg>
              </button>
              <button className="traffic-light traffic-minimize"
                onClick={() => minimizeWindow(win.id)} aria-label="Minimize">
                <svg width="6" height="2" viewBox="0 0 6 2">
                  <path d="M0.5 1H5.5" stroke="currentColor" strokeWidth="1.2" />
                </svg>
              </button>
              <button className="traffic-light traffic-maximize"
                onClick={handleMaximizeToggle} aria-label="Maximize">
                <svg width="6" height="6" viewBox="0 0 6 6">
                  <rect x="0.5" y="0.5" width="5" height="5" stroke="currentColor" strokeWidth="1" fill="none" />
                </svg>
              </button>
            </div>
            <span className="window-title">{win.title}</span>
            <div className="window-opacity-control"
              onMouseDown={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}>
              <input type="range" min="30" max="100"
                value={Math.round(win.opacity * 100)}
                onChange={(e) => updateWindowOpacity(win.id, Number(e.target.value) / 100)}
                className="window-opacity-slider"
                title={`Opacity: ${Math.round(win.opacity * 100)}%`} />
            </div>
          </div>
          <div className="window-content">{children}</div>
        </div>
      </Rnd>
    </>
  );
}
