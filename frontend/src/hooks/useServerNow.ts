import { useEffect, useState, useSyncExternalStore } from "react";
import {
  ensureServerClockSync,
  getServerOffsetMs,
  subscribeServerClock,
} from "../utils/serverClock";

const TICK_INTERVAL_MS = 15_000;

/**
 * Server-aligned "now" in seconds, at a 15s aging resolution. The offset
 * is read through useSyncExternalStore, so a sync completing at any
 * moment — including between render and effect subscription — applies on
 * the next render. The local half of the clock only advances on the 15s
 * tick, so the combined value may lag real time by up to one tick; that
 * is the accepted resolution for window aging.
 */
export function useServerNow(): number {
  const offsetMs = useSyncExternalStore(subscribeServerClock, getServerOffsetMs);
  const [localNowMs, setLocalNowMs] = useState(() => Date.now());

  useEffect(() => {
    ensureServerClockSync();
    const timer = setInterval(() => {
      setLocalNowMs(Date.now());
    }, TICK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  return (localNowMs + offsetMs) / 1000;
}
