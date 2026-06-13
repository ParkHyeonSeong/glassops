import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "../stores/authStore";

const WS_BASE = (
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`
).replace(/\/+$/, "");

const RECONNECT_BASE = 1000;
const MAX_RECONNECT_DELAY = 30000;

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
  const attempt = useRef(0);
  const endedRef = useRef(false);
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
      endedRef.current = false;  // fresh stream — clear any prior natural-end state

      const params = new URLSearchParams({
        agent_id: agentId,
        container_id: containerId,
        tail: String(tail),
      });
      // Token via subprotocol ("bearer, <token>") — keeps it out of the URL/logs.
      const token = useAuthStore.getState().accessToken;
      const ws = new WebSocket(
        `${WS_BASE}/docker/logs?${params.toString()}`,
        token ? ["bearer", token] : undefined,
      );
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        attempt.current = 0;  // reset backoff on a successful connection
        if (!unmounted) setStatus("streaming");
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (typeof msg.line === "string") {
            onLineRef.current(msg.line);
          } else if (msg.event === "end") {
            endedRef.current = true;  // natural end — don't reconnect on the close
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
        // Permanent auth/authz rejections for this token — stop reconnecting (this
        // hook has no refresh/logout) and surface it. 4003 = auth required, 4401 =
        // token revoked, 4403 = not allowed (non-admin / inactive / must-change).
        if (ev.code === 4003 || ev.code === 4401 || ev.code === 4403) {
          setStatus("error");
          setError(
            ev.code === 4401 ? "Session ended — please sign in again"
              : ev.code === 4403 ? "Access denied"
                : "Authentication required",
          );
          return;
        }
        if (ev.code === 4400) {
          setStatus("error");
          setError(ev.reason || "Bad request");
          return;
        }
        // Natural end → stop; don't re-stream the whole log on the close.
        if (endedRef.current) {
          setStatus("ended");
          return;
        }
        // Otherwise reconnect on unexpected close with exponential backoff + jitter
        // (capped) so a flapping stream doesn't hammer the server.
        setStatus("connecting");
        const delay = Math.min(MAX_RECONNECT_DELAY, RECONNECT_BASE * 2 ** attempt.current)
          + Math.random() * 1000;
        attempt.current += 1;
        reconnectRef.current = setTimeout(connect, delay);
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
