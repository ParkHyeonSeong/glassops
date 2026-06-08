import { useEffect, useRef } from "react";
import { useMetricsStore, type MetricSnapshot } from "../stores/metricsStore";
import { useAuthStore } from "../stores/authStore";

// Auto-detect WebSocket URL from current page location
const WS_URL = (
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`
).replace(/\/+$/, "");
const RECONNECT_DELAY = 3000;

interface WsMessage {
  agent_id: string;
  metrics: MetricSnapshot;
}

export function useWebSocket() {
  const pushMetrics = useMetricsStore((s) => s.pushMetrics);
  const setConnected = useMetricsStore((s) => s.setConnected);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const triedRefresh = useRef(false);

  useEffect(() => {
    let unmounted = false;

    function connect() {
      if (unmounted) return;

      // Pass the token as a subprotocol ("bearer, <token>") so it never appears in
      // the URL/query (which leaks into access logs); cookie is the fallback.
      const token = useAuthStore.getState().accessToken;
      const ws = new WebSocket(`${WS_URL}/client`, token ? ["bearer", token] : undefined);
      wsRef.current = ws;
      let opened = false;

      ws.onopen = () => {
        opened = true;
        triedRefresh.current = false;
        if (!unmounted) setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const msg: WsMessage = JSON.parse(event.data);
          if (msg.agent_id && msg.metrics) {
            pushMetrics(msg.agent_id, msg.metrics);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (unmounted) return;
        setConnected(false);
        // Closed before the handshake completed (no onopen) → likely an expired
        // token (or the server is down). Refresh once per failure streak, then
        // reconnect so the next attempt uses the new token. A live connection that
        // merely dropped (opened=true) just reconnects without refreshing.
        if (!opened && !triedRefresh.current) {
          triedRefresh.current = true;
          useAuthStore.getState().refresh().finally(() => {
            if (!unmounted) reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
          });
        } else {
          reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    connect();

    return () => {
      unmounted = true;
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [pushMetrics, setConnected]);
}
