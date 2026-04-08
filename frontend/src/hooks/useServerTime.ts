import { useState, useEffect, useRef, useCallback } from "react";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "";
const SYNC_INTERVAL = 60_000; // re-sync every 60s

/**
 * Returns a Date that tracks server time.
 * Fetches server UTC timestamp, computes offset from local clock (RTT-compensated).
 * Ticks every minute (at the minute boundary) since display is HH:MM.
 * Falls back to local time if the server is unreachable.
 */
export function useServerTime() {
  const [time, setTime] = useState(new Date());
  const offsetRef = useRef(0);

  const getServerTime = useCallback(
    () => new Date(Date.now() + offsetRef.current),
    []
  );

  useEffect(() => {
    let cancelled = false;

    const sync = async () => {
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
        const after = Date.now();
        const rtt = after - before;
        const serverMs = (data as { timestamp: number }).timestamp * 1000 + rtt / 2;
        if (!cancelled) {
          offsetRef.current = serverMs - after;
        }
      } catch {
        // keep current offset (0 = local time if never synced)
      }
    };

    sync();
    const syncTimer = setInterval(sync, SYNC_INTERVAL);

    // Tick at next minute boundary, then every 60s
    const now = getServerTime();
    const msUntilNextMinute =
      (60 - now.getSeconds()) * 1000 - now.getMilliseconds();

    let minuteInterval: ReturnType<typeof setInterval> | null = null;

    const alignTimeout = setTimeout(() => {
      if (cancelled) return;
      setTime(getServerTime());
      minuteInterval = setInterval(() => {
        if (!cancelled) setTime(getServerTime());
      }, 60_000);
    }, msUntilNextMinute);

    return () => {
      cancelled = true;
      clearInterval(syncTimer);
      clearTimeout(alignTimeout);
      if (minuteInterval) clearInterval(minuteInterval);
    };
  }, [getServerTime]);

  return time;
}
