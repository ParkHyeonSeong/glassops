import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useAuthStore } from "../../stores/authStore";
import "xterm/css/xterm.css";

const WS_URL =
  import.meta.env.VITE_WS_URL ||
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

export default function TerminalApp() {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const accessToken = useAuthStore((s) => s.accessToken);

  useEffect(() => {
    if (!containerRef.current) return;

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
    termRef.current = term;

    // WebSocket connection with JWT token
    const ws = new WebSocket(`${WS_URL}/terminal?token=${encodeURIComponent(accessToken || "")}`);
    wsRef.current = ws;

    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      term.writeln("\x1b[36mConnected to GlassOps Terminal\x1b[0m");
      term.writeln("");
      // Send initial resize
      ws.send(
        JSON.stringify({
          type: "resize",
          rows: term.rows,
          cols: term.cols,
        })
      );
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

    ws.onclose = () => {
      term.writeln("\r\n\x1b[31mDisconnected\x1b[0m");
    };

    // Terminal input → WebSocket
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data));
      }
    });

    // Resize handling
    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            type: "resize",
            rows: term.rows,
            cols: term.cols,
          })
        );
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      ws.close();
      term.dispose();
    };
  }, []);

  return (
    <div
      ref={containerRef}
      className="terminal-container"
      style={{ width: "100%", height: "100%", padding: 4 }}
    />
  );
}
