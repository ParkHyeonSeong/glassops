import { useEffect, useMemo, useRef } from "react";
import { Terminal as XTerm } from "xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useAuthStore } from "../../stores/authStore";
import { useMetricsStore } from "../../stores/metricsStore";
import "xterm/css/xterm.css";

const WS_URL =
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

export default function TerminalApp() {
  const containerRef = useRef<HTMLDivElement>(null);
  const accessToken = useAuthStore((s) => s.accessToken);
  const hostAccounts = useAuthStore((s) => s.hostAccounts);
  const agentId = useMetricsStore((s) => s.agentId);

  // The user can only open a shell on hosts they have a mapping for. The local
  // agent gets an env-var fallback on the backend, so allow it through unconditionally.
  const hasAccess = useMemo(() => {
    if (!agentId) return false;
    if (Object.prototype.hasOwnProperty.call(hostAccounts, agentId) && hostAccounts[agentId]) {
      return true;
    }
    // Local-agent fallback: the backend may use GLASSOPS_TERMINAL_USER. We can't tell from
    // here, so optimistically allow it; the backend will close the WS if it can't spawn.
    return agentId === "local";
  }, [agentId, hostAccounts]);

  useEffect(() => {
    if (!containerRef.current || !agentId) return;

    const term = new XTerm({
      theme: {
        background: "#1a1a2e",
        foreground: "#e0e0e0",
        cursor: "#4facfe",
        selectionBackground: "rgba(79, 172, 254, 0.3)",
        black: "#1a1a2e",
        red: "#f85032",
        green: "#43e97b",
        yellow: "#f7971e",
        blue: "#4facfe",
        magenta: "#a18cd1",
        cyan: "#4facfe",
        white: "#e0e0e0",
      },
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      cursorStyle: "bar",
      scrollback: 5000,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(containerRef.current);
    fitAddon.fit();
    term.focus();

    if (!hasAccess) {
      term.writeln(`\x1b[31mNo terminal access on '${agentId}'.\x1b[0m`);
      term.writeln("Ask an admin to map a host account for you in Users.");
      return () => { term.dispose(); };
    }

    const params = new URLSearchParams({ agent_id: agentId });
    if (accessToken) params.set("token", accessToken);
    const ws = new WebSocket(`${WS_URL}/terminal?${params.toString()}`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      term.writeln(`\x1b[36mConnected to ${agentId}\x1b[0m`);
      term.writeln("");
      ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    };

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(event.data));
      } else if (typeof event.data === "string") {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "timeout") {
            term.writeln(`\r\n\x1b[33m${msg.message}\x1b[0m`);
          }
        } catch {
          term.write(event.data);
        }
      }
    };

    ws.onclose = (ev) => {
      const reason = ev.reason || "Disconnected";
      term.writeln(`\r\n\x1b[31m${reason}\x1b[0m`);
    };

    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      try { ws.close(); } catch { /* ignore */ }
      term.dispose();
    };
  }, [agentId, accessToken, hasAccess]);

  return (
    <div
      ref={containerRef}
      className="terminal-container"
      style={{ width: "100%", height: "100%", padding: 4 }}
    />
  );
}
