export interface WindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WindowState {
  id: string;
  appId: string;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  minWidth: number;
  minHeight: number;
  isMinimized: boolean;
  isMaximized: boolean;
  zIndex: number;
  opacity: number;
  preMaximizeBounds: WindowBounds | null;
  // Multi-instance apps (e.g. per-container log/metric windows) carry an
  // instance key here; single-instance apps leave it undefined.
  params?: Record<string, string>;
}

export interface AppDefinition {
  id: string;
  title: string;
  icon: string;
  defaultWidth: number;
  defaultHeight: number;
  minWidth: number;
  minHeight: number;
  adminOnly?: boolean;
  // When true, openWindow allows multiple windows for the same appId provided
  // their `params.key` (or the full params dict) differs.
  multiInstance?: boolean;
  // Hide from the launcher/dock — these windows are only opened from inside
  // another app (e.g. Docker Manager).
  hiddenFromLauncher?: boolean;
}

export type ConnectionStatus = "connected" | "disconnected" | "connecting";
