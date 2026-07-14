const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";
const SYNC_INTERVAL_MS = 60_000;

type ClockListener = () => void;

let offsetMs = 0;
let syncGeneration = 0;
let syncTimer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<ClockListener>();

/**
 * Single source of truth for the RTT-compensated server clock offset.
 * `/api/time` is auth-exempt so plain fetch is fine. Sync failures keep
 * the last known offset (0 = local clock), so every consumer degrades to
 * browser time gracefully.
 */
export async function syncServerClock(): Promise<void> {
  const generation = ++syncGeneration;
  try {
    const before = Date.now();
    const res = await fetch(`${BACKEND_URL}/api/time`);
    if (!res.ok) return;
    const data: unknown = await res.json();
    if (
      typeof data !== "object" ||
      data === null ||
      !("timestamp" in data) ||
      typeof (data as { timestamp: unknown }).timestamp !== "number"
    ) {
      return;
    }
    // A newer sync started while this one was in flight — drop the stale
    // measurement so it cannot overwrite a fresher offset.
    if (generation !== syncGeneration) return;
    const after = Date.now();
    const rtt = after - before;
    offsetMs = (data as { timestamp: number }).timestamp * 1000 + rtt / 2 - after;
    for (const listener of listeners) listener();
  } catch {
    // keep last known offset; 0 means local-clock fallback
  }
}

export function ensureServerClockSync(): void {
  if (syncTimer !== null) return;
  void syncServerClock();
  syncTimer = setInterval(() => {
    void syncServerClock();
  }, SYNC_INTERVAL_MS);
}

export function subscribeServerClock(listener: ClockListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getServerOffsetMs(): number {
  return offsetMs;
}

export function serverNowSeconds(): number {
  return (Date.now() + offsetMs) / 1000;
}

export function _resetServerClockForTest(): void {
  offsetMs = 0;
  syncGeneration += 1;
  if (syncTimer !== null) {
    clearInterval(syncTimer);
    syncTimer = null;
  }
  listeners.clear();
}
