/* Shared UI primitives for Docker apps/windows. Pure components only —
   utilities, hooks, and types live in `dockerSharedUtils.ts` so this file
   stays fast-refresh-friendly. */

import { Play, Square, RotateCw } from "lucide-react";
import type { ContainerAction } from "./dockerSharedUtils";

export function StatusBadge({ status }: { status: string }) {
  const isRunning = status === "running";
  const color = isRunning ? "var(--color-success)" : "var(--color-danger)";
  return (
    <span className="docker-status" style={{ color }}>
      <span className="docker-status-dot" style={{ background: color }} />
      {status}
    </span>
  );
}

export function ContainerActionButtons({
  status,
  removed,
  loading,
  onAction,
}: {
  status: string | undefined;
  removed: boolean;
  loading: boolean;
  onAction: (action: ContainerAction) => void;
}) {
  if (removed) return null;
  const isRunning = status === "running";
  return (
    <>
      {isRunning ? (
        <>
          <button className="docker-action-btn docker-action-stop"
            onClick={() => onAction("stop")} disabled={loading} title="Stop">
            <Square size={13} />
          </button>
          <button className="docker-action-btn docker-action-restart"
            onClick={() => onAction("restart")} disabled={loading} title="Restart">
            <RotateCw size={13} />
          </button>
        </>
      ) : (
        <button className="docker-action-btn docker-action-start"
          onClick={() => onAction("start")} disabled={loading} title="Start">
          <Play size={13} />
        </button>
      )}
    </>
  );
}

export function ContainerRemovedBanner({ containerName }: { containerName: string }) {
  return (
    <div className="docker-removed-banner">
      Container <code>{containerName}</code> is no longer present on this host. Close this window when you're done.
    </div>
  );
}
