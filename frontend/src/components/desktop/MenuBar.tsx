import { useState, useEffect, useMemo, useRef, useId } from "react";
import { Wifi, WifiOff, ChevronDown, Check } from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import type { ConnectionStatus } from "../../types";
import { useMetricsStore } from "../../stores/metricsStore";

interface MenuBarProps {
  connectionStatus: ConnectionStatus;
  cpuPercent?: number;
  memPercent?: number;
}

export default function MenuBar({
  connectionStatus,
  cpuPercent,
  memPercent,
}: MenuBarProps) {
  const [time, setTime] = useState(new Date());
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const [focusedIndex, setFocusedIndex] = useState(0);
  const agentSelectRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const focusedItemRef = useRef<HTMLLIElement>(null);
  const listboxId = useId();
  const agentIds = useMetricsStore(useShallow((s) => s.agentIds));
  const selectedAgentId = useMetricsStore((s) => s.agentId);
  const selectAgent = useMetricsStore((s) => s.selectAgent);

  const sortedAgentIds = useMemo(
    () =>
      [...agentIds].sort((a, b) =>
        a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }),
      ),
    [agentIds],
  );

  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!agentMenuOpen) return;
    const onMouseDown = (e: MouseEvent) => {
      if (
        agentSelectRef.current &&
        !agentSelectRef.current.contains(e.target as Node)
      ) {
        setAgentMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [agentMenuOpen]);

  useEffect(() => {
    if (!agentMenuOpen) return;
    focusedItemRef.current?.scrollIntoView({ block: "nearest" });
  }, [agentMenuOpen, focusedIndex]);

  const openMenu = () => {
    const idx = sortedAgentIds.findIndex((id) => id === selectedAgentId);
    setFocusedIndex(idx >= 0 ? idx : 0);
    setAgentMenuOpen(true);
  };

  const closeMenu = () => {
    setAgentMenuOpen(false);
    triggerRef.current?.focus();
  };

  const commitSelection = (idx: number) => {
    const id = sortedAgentIds[idx];
    if (!id) return;
    selectAgent(id);
    closeMenu();
  };

  const onTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!agentMenuOpen) {
      if (
        e.key === "ArrowDown" ||
        e.key === "ArrowUp" ||
        e.key === "Enter" ||
        e.key === " "
      ) {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    const last = sortedAgentIds.length - 1;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setFocusedIndex((i) => (i >= last ? 0 : i + 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setFocusedIndex((i) => (i <= 0 ? last : i - 1));
        break;
      case "Home":
        e.preventDefault();
        setFocusedIndex(0);
        break;
      case "End":
        e.preventDefault();
        setFocusedIndex(last);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        commitSelection(focusedIndex);
        break;
      case "Escape":
        e.preventDefault();
        closeMenu();
        break;
      case "Tab":
        setAgentMenuOpen(false);
        break;
    }
  };

  const statusColor =
    connectionStatus === "connected"
      ? "var(--color-success)"
      : connectionStatus === "connecting"
        ? "var(--color-warning)"
        : "var(--color-danger)";

  const StatusIcon = connectionStatus === "disconnected" ? WifiOff : Wifi;

  return (
    <div className="menubar">
      <div className="menubar-left">
        <span className="menubar-logo">GlassOps</span>
      </div>

      <div className="menubar-center">
        <StatusIcon size={13} style={{ color: statusColor }} />
        {agentIds.length > 1 ? (
          <div className="menubar-agent-select" ref={agentSelectRef}>
            <button
              ref={triggerRef}
              type="button"
              className="menubar-agent-trigger"
              aria-haspopup="listbox"
              aria-expanded={agentMenuOpen}
              aria-controls={agentMenuOpen ? listboxId : undefined}
              aria-activedescendant={
                agentMenuOpen ? `${listboxId}-opt-${focusedIndex}` : undefined
              }
              onClick={() => (agentMenuOpen ? closeMenu() : openMenu())}
              onKeyDown={onTriggerKeyDown}
            >
              <span className="menubar-agent-trigger-label">
                {selectedAgentId ?? "Select agent"}
              </span>
              <ChevronDown
                size={11}
                className="menubar-agent-trigger-chevron"
              />
            </button>
            {agentMenuOpen && (
              <ul
                id={listboxId}
                className="menubar-agent-menu"
                role="listbox"
                aria-activedescendant={`${listboxId}-opt-${focusedIndex}`}
              >
                {sortedAgentIds.map((id, idx) => {
                  const isSelected = id === selectedAgentId;
                  const isFocused = idx === focusedIndex;
                  return (
                    <li
                      key={id}
                      ref={isFocused ? focusedItemRef : undefined}
                      id={`${listboxId}-opt-${idx}`}
                      role="option"
                      aria-selected={isSelected}
                      className={`menubar-agent-item${
                        isSelected ? " menubar-agent-item--selected" : ""
                      }${isFocused ? " menubar-agent-item--focused" : ""}`}
                      onMouseEnter={() => setFocusedIndex(idx)}
                      onClick={() => commitSelection(idx)}
                    >
                      <span className="menubar-agent-item-label">{id}</span>
                      {isSelected && (
                        <Check size={11} className="menubar-agent-item-check" />
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ) : (
          <span className="menubar-server">
            {selectedAgentId ?? "No Agent"}
          </span>
        )}
        <span
          className="menubar-status-dot"
          style={{ backgroundColor: statusColor }}
        />
      </div>

      <div className="menubar-right">
        {cpuPercent !== undefined && (
          <span className="menubar-metric">CPU {cpuPercent}%</span>
        )}
        {memPercent !== undefined && (
          <span className="menubar-metric">MEM {memPercent}%</span>
        )}
        <span className="menubar-time">
          {time.toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </div>
    </div>
  );
}
