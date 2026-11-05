import { create } from "zustand";

export const WALLPAPERS = [
  { id: "default", label: "Deep Space", css: "linear-gradient(135deg, #1a1a2e, #16213e)" },
  { id: "ocean", label: "Ocean", css: "linear-gradient(135deg, #0f2027, #203a43, #2c5364)" },
  { id: "sunset", label: "Sunset", css: "linear-gradient(135deg, #2d1b69, #6b2fa0, #c4256a)" },
  { id: "forest", label: "Forest", css: "linear-gradient(135deg, #0a1a0f, #1b3a2d, #2d5a3f)" },
  { id: "midnight", label: "Midnight", css: "linear-gradient(135deg, #0c0c1d, #1a1a3a)" },
];

// ===== Dock =====
// Whatever comes from localStorage or from setDock goes through sanitizeDockSettings,
// so a hand-edited or stale value can never reach the dock.

const DOCK_THEMES = ["dark", "light"] as const;
const DOCK_POSITIONS = ["bottom", "top", "left", "right"] as const;
const DOCK_SPRING_NAMES = ["smooth", "snappy", "bouncy", "gentle"] as const;

export type DockTheme = (typeof DOCK_THEMES)[number];
export type DockPosition = (typeof DOCK_POSITIONS)[number];
export type DockSpring = (typeof DOCK_SPRING_NAMES)[number];

export interface DockSettings {
  theme: DockTheme;
  accentColor: string; // #rrggbb
  badgeColor: string; // #rrggbb
  position: DockPosition; // screen edge the dock sits on
  baseItemSize: number; // px
  magnification: number; // px, size of the item under the pointer
  distance: number; // px, how far the magnification reaches along the dock
  panelHeight: number; // px, bar thickness
  gap: number; // px
  roundness: number; // 0-1
  spring: DockSpring;
  bounce: boolean;
  autoHide: boolean;
  tiles: boolean; // false draws bare icons without a tile behind them
  showLabels: boolean;
  showIndicators: boolean;
  showBadges: boolean;
}

export const DOCK_DEFAULTS: DockSettings = {
  theme: "dark",
  accentColor: "#0a84ff",
  badgeColor: "#ff453a",
  position: "bottom",
  baseItemSize: 50,
  magnification: 70,
  distance: 200,
  panelHeight: 68,
  gap: 10,
  roundness: 0.5,
  spring: "smooth",
  bounce: true,
  autoHide: false,
  tiles: true,
  showLabels: true,
  showIndicators: true,
  showBadges: true,
};

// min/max bound the sanitiser; step is only used by the Settings sliders.
export const DOCK_RANGES: Record<
  "baseItemSize" | "magnification" | "distance" | "panelHeight" | "gap" | "roundness",
  { min: number; max: number; step: number }
> = {
  baseItemSize: { min: 30, max: 80, step: 2 },
  magnification: { min: 30, max: 140, step: 2 },
  distance: { min: 60, max: 400, step: 10 },
  panelHeight: { min: 30, max: 120, step: 2 },
  gap: { min: 0, max: 30, step: 1 },
  roundness: { min: 0, max: 1, step: 0.05 },
};

// Damped-spring presets for the magnification motion.
export const DOCK_SPRINGS: Record<DockSpring, { mass: number; stiffness: number; damping: number }> = {
  smooth: { mass: 0.1, stiffness: 150, damping: 12 },
  snappy: { mass: 0.1, stiffness: 420, damping: 20 },
  bouncy: { mass: 0.1, stiffness: 220, damping: 4 },
  gentle: { mass: 0.2, stiffness: 120, damping: 14 },
};

const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function pickEnum<T extends string>(value: unknown, options: readonly T[], fallback: T): T {
  return options.find((option) => option === value) ?? fallback;
}

function pickColor(value: unknown, fallback: string): string {
  return typeof value === "string" && HEX_COLOR.test(value) ? value : fallback;
}

function pickNumber(value: unknown, range: { min: number; max: number }, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? clamp(value, range.min, range.max) : fallback;
}

function pickBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

// Keeps a value only when it has the right type (an enum member, a #rrggbb string, a
// finite number clamped into its range, or a boolean) and falls back to the default
// otherwise. Anything that is not a plain object yields the defaults, and unknown keys
// are dropped. Keys come out in DOCK_DEFAULTS order so the stored JSON is stable.
export function sanitizeDockSettings(raw: unknown): DockSettings {
  const src: Record<string, unknown> =
    typeof raw === "object" && raw !== null && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const d = DOCK_DEFAULTS;
  return {
    theme: pickEnum(src.theme, DOCK_THEMES, d.theme),
    accentColor: pickColor(src.accentColor, d.accentColor),
    badgeColor: pickColor(src.badgeColor, d.badgeColor),
    position: pickEnum(src.position, DOCK_POSITIONS, d.position),
    baseItemSize: pickNumber(src.baseItemSize, DOCK_RANGES.baseItemSize, d.baseItemSize),
    magnification: pickNumber(src.magnification, DOCK_RANGES.magnification, d.magnification),
    distance: pickNumber(src.distance, DOCK_RANGES.distance, d.distance),
    panelHeight: pickNumber(src.panelHeight, DOCK_RANGES.panelHeight, d.panelHeight),
    gap: pickNumber(src.gap, DOCK_RANGES.gap, d.gap),
    roundness: pickNumber(src.roundness, DOCK_RANGES.roundness, d.roundness),
    spring: pickEnum(src.spring, DOCK_SPRING_NAMES, d.spring),
    bounce: pickBoolean(src.bounce, d.bounce),
    autoHide: pickBoolean(src.autoHide, d.autoHide),
    tiles: pickBoolean(src.tiles, d.tiles),
    showLabels: pickBoolean(src.showLabels, d.showLabels),
    showIndicators: pickBoolean(src.showIndicators, d.showIndicators),
    showBadges: pickBoolean(src.showBadges, d.showBadges),
  };
}

interface SettingsStore {
  wallpaper: string;
  setWallpaper: (id: string) => void;
  dock: DockSettings;
  setDock: (patch: Partial<DockSettings>) => void;
  resetDock: () => void;
}

function loadSetting<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`glassops_${key}`);
    return v ? JSON.parse(v) : fallback;
  } catch { return fallback; }
}

export function persistSetting(key: string, value: unknown): boolean {
  try {
    localStorage.setItem(`glassops_${key}`, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

// Alert thresholds used to live here under `glassops_thresholds`, in parallel with
// (and disagreeing with) useThresholdsStore. They now live only in useThresholdsStore;
// see lib/thresholdMigration.ts for the one-shot migration off the old key.
export const useSettingsStore = create<SettingsStore>((set, get) => ({
  wallpaper: loadSetting("wallpaper", "default"),
  dock: sanitizeDockSettings(loadSetting<unknown>("dock", null)),

  setWallpaper: (id) => {
    set({ wallpaper: id });
    persistSetting("wallpaper", id);
  },

  setDock: (patch) => {
    const dock = sanitizeDockSettings({ ...get().dock, ...patch });
    set({ dock });
    persistSetting("dock", dock);
  },

  resetDock: () => get().setDock(DOCK_DEFAULTS),
}));
