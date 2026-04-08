import { useEffect, useRef } from "react";
import { useMetricsStore, type MetricSnapshot } from "../stores/metricsStore";

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

  useEffect(() => {
    let unmounted = false;

    function connect() {
      if (unmounted) return;

      const ws = new WebSocket(`${WS_URL}/client`);
      wsRef.current = ws;

      ws.onopen = () => {
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
        if (!unmounted) {
          setConnected(false);
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
