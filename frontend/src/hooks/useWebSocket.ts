import { useEffect, useRef } from "react";
import { useMetricsStore, type MetricSnapshot } from "../stores/metricsStore";
import { useAuthStore } from "../stores/authStore";
import { useAlertStore } from "../stores/alertStore";
import { fetchWithAuth } from "../utils/api";

// Auto-detect WebSocket URL from current page location
const WS_URL = (
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`
).replace(/\/+$/, "");
const RECONNECT_BASE = 1000;
const MAX_RECONNECT_DELAY = 30000;
const AUTH_REJECT_CODES = [4001, 4003, 4403];

interface WsMessage {
  agent_id: string;
  metrics: MetricSnapshot;
}

export function useWebSocket() {
  const pushMetrics = useMetricsStore((s) => s.pushMetrics);
  const setConnected = useMetricsStore((s) => s.setConnected);
  const mergeHistory = useMetricsStore((s) => s.mergeHistory);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const triedRefresh = useRef(false);
  const attempt = useRef(0);

  useEffect(() => {
    let unmounted = false;
    let abortHistory: (() => void) | undefined;

    function dispose() {
      unmounted = true;
      clearTimeout(reconnectTimer.current);
      abortHistory?.();
      wsRef.current?.close();
    }
    // Invalidate synchronously on logout, before a queued REST response can
    // apply to another login. Token refresh inside the same session is allowed.
    const unsubscribeAuth = useAuthStore.subscribe((state, previous) => {
      if (!state.isAuthenticated || state.email !== previous.email) dispose();
    });

    function connect() {
      if (unmounted) return;

      // Pass the token as a subprotocol ("bearer, <token>") so it never appears in
      // the URL/query (which leaks into access logs); cookie is the fallback.
      const token = useAuthStore.getState().accessToken;
      const ws = new WebSocket(`${WS_URL}/client`, token ? ["bearer", token] : undefined);
      wsRef.current = ws;
      let opened = false;
      let closed = false;
      let warned = false;
      const requested = new Set<string>();
      const controller = new AbortController();
      abortHistory = () => controller.abort();
      const currentConnection = () => !unmounted && wsRef.current === ws;
      const active = () => currentConnection() && !closed && !controller.signal.aborted;

      async function restoreHistory(agentId: string) {
        if (requested.has(agentId)) return;
        requested.add(agentId);
        try {
          const response = await fetchWithAuth(
            `/api/metrics/${encodeURIComponent(agentId)}/history?limit=120`,
            { signal: controller.signal },
          );
          if (!response.ok) throw new Error(`History HTTP ${response.status}`);
          const payload = await response.json();
          if (payload.agent_id !== agentId || !Array.isArray(payload.metrics)) throw new Error("Invalid history response");
          if (active()) mergeHistory(agentId, payload.metrics);
        } catch {
          if (active() && !warned) {
            warned = true;
            useAlertStore.getState().push("warning", "Recent history could not be restored. Live updates are continuing.", "live-history");
          }
        }
      }

      ws.onopen = () => {
        if (!active()) return;
        opened = true;
        triedRefresh.current = false;
        attempt.current = 0;  // reset backoff on a successful connection
        if (!unmounted) setConnected(true);
      };

      ws.onmessage = (event) => {
        if (!active()) return;
        try {
          const msg: WsMessage = JSON.parse(event.data);
          if (msg.agent_id && msg.metrics) {
            pushMetrics(msg.agent_id, msg.metrics);
            void restoreHistory(msg.agent_id);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = (ev: CloseEvent) => {
        closed = true;
        controller.abort();
        if (!currentConnection()) return;
        setConnected(false);
        // 4401 = token revoked (logout / password / role change / deactivation). The
        // server closes with this BEFORE the handshake opens, and a refresh can't
        // recover a revoked session (the refresh token is invalidated too), so log out
        // immediately rather than burning a refresh round-trip + reconnect first.
        if (ev.code === 4401) {
          useAuthStore.getState().logout();
          return;
        }
        const authReject = AUTH_REJECT_CODES.includes(ev.code);
        // Auth rejected on a live connection, or still rejected after we refreshed →
        // re-login (don't keep reconnecting with a known-bad token).
        if (authReject && (opened || triedRefresh.current)) {
          useAuthStore.getState().logout();
          return;
        }
        // Closed before the handshake completed (no onopen) → likely an expired
        // token. Refresh once per failure streak, then reconnect with the new token.
        if (!opened && !triedRefresh.current) {
          triedRefresh.current = true;
          useAuthStore.getState().refresh().finally(() => {
            if (currentConnection()) reconnectTimer.current = setTimeout(connect, RECONNECT_BASE);
          });
          return;
        }
        // Otherwise reconnect with exponential backoff + jitter, capped, so a down or
        // rejecting server isn't hit every 3s forever.
        const delay = Math.min(MAX_RECONNECT_DELAY, RECONNECT_BASE * 2 ** attempt.current)
          + Math.random() * 1000;
        attempt.current += 1;
        reconnectTimer.current = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        // The browser fires onclose right after onerror — let onclose own the
        // reconnect/backoff so we don't double up the close path.
      };
    }

    connect();

    return () => {
      unsubscribeAuth();
      dispose();
    };
  }, [pushMetrics, setConnected, mergeHistory]);
}
