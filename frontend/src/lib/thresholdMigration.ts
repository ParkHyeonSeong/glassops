import { type MetricKey, type Threshold, DEFAULT_THRESHOLDS } from "./thresholds";

// The settingsStore era wrote a flat {cpuWarn,cpuCrit,…} object here. The current
// store is zustand-persisted under CURRENT_KEY with a {state:{…}} envelope.
export const LEGACY_KEY = "glassops_thresholds";
export const CURRENT_KEY = "glassops-thresholds";

interface LegacyShape {
  cpuWarn: number; cpuCrit: number; memWarn: number; memCrit: number; diskCrit: number;
}

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function clamp(v: number): number {
  return Math.min(100, Math.max(0, v));
}

function pair(warn: number, crit: number): Threshold {
  const w = clamp(warn);
  const c = clamp(crit);
  return { warn: Math.min(w, c), crit: Math.max(w, c) };
}

export function hasCurrentThresholds(): boolean {
  try {
    return localStorage.getItem(CURRENT_KEY) !== null;
  } catch {
    return false;
  }
}

export function readLegacyThresholds(): Partial<Record<MetricKey, Threshold>> | null {
  let raw: string | null;
  try {
    raw = localStorage.getItem(LEGACY_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const legacy = parsed as Partial<LegacyShape>;
  const out: Partial<Record<MetricKey, Threshold>> = {};
  if (isNum(legacy.cpuWarn) && isNum(legacy.cpuCrit)) out.cpu = pair(legacy.cpuWarn, legacy.cpuCrit);
  if (isNum(legacy.memWarn) && isNum(legacy.memCrit)) out.mem = pair(legacy.memWarn, legacy.memCrit);
  // No legacy diskWarn existed. Anchor warn to the default but never above the
  // migrated crit, or clampThreshold would silently swap the two.
  if (isNum(legacy.diskCrit)) {
    const crit = clamp(legacy.diskCrit);
    out.disk = { warn: Math.min(DEFAULT_THRESHOLDS.disk.warn, crit), crit };
  }
  return Object.keys(out).length > 0 ? out : null;
}

export function clearLegacyThresholds(): void {
  try {
    localStorage.removeItem(LEGACY_KEY);
  } catch {
    // Storage unavailable — there is nothing to retire.
  }
}

/**
 * One-shot retirement of the legacy key. Applies the legacy values only when the
 * current store has never been written, so an existing System Monitor setup wins.
 * The legacy key is always removed afterwards — this migration does not run twice.
 */
export function migrateLegacyThresholds(
  apply: (patch: Partial<Record<MetricKey, Threshold>>) => void,
): void {
  if (!hasCurrentThresholds()) {
    const legacy = readLegacyThresholds();
    if (legacy) apply(legacy);
  }
  clearLegacyThresholds();
}
