import { useState, useEffect, useCallback } from "react";
import { ensureServerClockSync, getServerOffsetMs } from "../utils/serverClock";

/**
 * Returns a Date that tracks server time.
 * Offset sync (RTT-compensated, 60s re-sync) is delegated to the shared
 * serverClock module so metric windows and clock displays agree.
 * Ticks every minute (at the minute boundary) since display is HH:MM.
 * Falls back to local time if the server is unreachable.
 */
export function useServerTime() {
  const [time, setTime] = useState(() => new Date(Date.now() + getServerOffsetMs()));

  const getServerTime = useCallback(
    () => new Date(Date.now() + getServerOffsetMs()),
    []
  );

  useEffect(() => {
    ensureServerClockSync();
    let cancelled = false;

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
      clearTimeout(alignTimeout);
      if (minuteInterval) clearInterval(minuteInterval);
    };
  }, [getServerTime]);

  return time;
}
