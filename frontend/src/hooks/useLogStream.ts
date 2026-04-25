import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "../stores/authStore";

const WS_BASE = (
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`
).replace(/\/+$/, "");

const RECONNECT_DELAY = 3000;

export type LogStreamStatus = "idle" | "connecting" | "streaming" | "ended" | "error";

interface Options {
  containerId: string | null;
  agentId: string | null;
  tail?: number;
  enabled: boolean;
  onLine: (chunk: string) => void;
}

/**
 * Streams Docker container logs over WebSocket. Reconnects on transient errors.
 * The `enabled` flag lets the caller toggle streaming without unmounting.
 */
export function useLogStream({ containerId, agentId, tail = 300, enabled, onLine }: Options) {
  const [rawStatus, setStatus] = useState<LogStreamStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const onLineRef = useRef(onLine);

  useEffect(() => {
    onLineRef.current = onLine;
  }, [onLine]);

  useEffect(() => {
    if (!enabled || !containerId || !agentId) return;

    let unmounted = false;

    function connect() {
      if (unmounted || !containerId || !agentId) return;
      setError(null);

      const params = new URLSearchParams({
        agent_id: agentId,
        container_id: containerId,
        tail: String(tail),
      });
      const token = useAuthStore.getState().accessToken;
      if (token) params.set("token", token);

      const ws = new WebSocket(`${WS_BASE}/docker/logs?${params.toString()}`);
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (!unmounted) setStatus("streaming");
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (typeof msg.line === "string") {
            onLineRef.current(msg.line);
          } else if (msg.event === "end") {
            setStatus("ended");
          } else if (msg.event === "error") {
            setStatus("error");
            setError(typeof msg.error === "string" ? msg.error : "Stream error");
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onerror = () => {
        // Browsers do not expose error details; let onclose handle the state transition.
      };

      ws.onclose = (ev) => {
        if (unmounted) return;
        if (ev.code === 4003) {
          setStatus("error");
          setError("Authentication required");
          return;
        }
        if (ev.code === 4400) {
          setStatus("error");
          setError(ev.reason || "Bad request");
          return;
        }
        // Reconnect on unexpected close unless we already ended.
        setStatus((s) => (s === "ended" ? s : "connecting"));
        reconnectRef.current = setTimeout(connect, RECONNECT_DELAY);
      };
    }

    connect();

    return () => {
      unmounted = true;
      clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [containerId, agentId, tail, enabled]);

  // Derive an "idle" status when streaming is disabled instead of setting state in effect.
  const status: LogStreamStatus = enabled && containerId && agentId ? rawStatus : "idle";

  return { status, error };
}
