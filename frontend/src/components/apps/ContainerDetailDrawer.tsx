import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { fetchWithAuth } from "../../utils/api";
import { StatusBadge, ContainerActionButtons } from "./dockerShared";
import type { ContainerAction } from "./dockerSharedUtils";

interface ContainerDetail {
  ok: boolean;
  id: string;
  name: string;
  image: string;
  status: string;
  created: string;
  ports: Record<string, { HostIp: string; HostPort: string }[] | null>;
  env: string[];
  mounts: { source: string; destination: string; mode: string }[];
  networks: string[];
}

export default function ContainerDetailDrawer({
  containerId,
  status,
  onClose,
  onAction,
  actionLoading,
}: {
  containerId: string;
  status: string | undefined;
  onClose: () => void;
  onAction: (id: string, action: ContainerAction) => void;
  actionLoading: boolean;
}) {
  const [detail, setDetail] = useState<ContainerDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    setDetail(null);
    // agent_id is auto-injected by fetchWithAuth for remote agents, mirroring
    // the container action call in DockerManager.
    fetchWithAuth(`/api/docker/containers/${containerId}`)
      .then((r) => r.json())
      .then((d: ContainerDetail) => {
        if (ignore) return;
        if (d.ok) setDetail(d);
        else setError("Container not found");
      })
      .catch(() => { if (!ignore) setError("Failed to load details"); })
      .finally(() => { if (!ignore) setLoading(false); });
    return () => { ignore = true; };
  }, [containerId]);

  const shownStatus = detail?.status ?? status;

  return (
    <div className="docker-drawer">
      <div className="docker-drawer-head">
        <div className="docker-drawer-title">
          <span className="docker-drawer-name">{detail?.name ?? "Container"}</span>
          {shownStatus && <StatusBadge status={shownStatus} />}
        </div>
        <div className="docker-drawer-head-actions">
          <ContainerActionButtons
            status={shownStatus}
            removed={false}
            loading={actionLoading}
            onAction={(action) => onAction(containerId, action)}
          />
          <button className="docker-drawer-close" onClick={onClose} title="Close"><X size={15} /></button>
        </div>
      </div>

      <div className="docker-drawer-body">
        {loading && <p className="docker-drawer-loading">Loading…</p>}
        {error && <p className="docker-drawer-error">{error}</p>}
        {detail && (
          <>
            <Section title="General">
              <KV k="Image" v={detail.image} />
              <KV k="Status" v={detail.status} />
              <KV k="Created" v={detail.created} />
              <KV k="ID" v={detail.id} />
            </Section>

            <Section title="Ports">
              {Object.keys(detail.ports || {}).length === 0
                ? <p className="docker-drawer-empty">No published ports</p>
                : Object.entries(detail.ports).map(([cport, binds]) => (
                    <KV key={cport} k={cport}
                      v={binds && binds.length
                        ? binds.map((b) => `${b.HostIp || "0.0.0.0"}:${b.HostPort}`).join(", ")
                        : "—"} />
                  ))}
            </Section>

            <Section title="Networks">
              {detail.networks.length === 0
                ? <p className="docker-drawer-empty">None</p>
                : <div className="docker-drawer-chips">
                    {detail.networks.map((n) => <span key={n} className="docker-drawer-chip">{n}</span>)}
                  </div>}
            </Section>

            <Section title="Mounts">
              {detail.mounts.length === 0
                ? <p className="docker-drawer-empty">None</p>
                : detail.mounts.map((m, i) => (
                    <KV key={i} k={m.source} v={`${m.destination}${m.mode ? ` (${m.mode})` : ""}`} />
                  ))}
            </Section>

            <Section title="Environment">
              {detail.env.length === 0
                ? <p className="docker-drawer-empty">None</p>
                : <div className="docker-drawer-env">
                    {detail.env.map((e, i) => <code key={i} className="docker-drawer-envline">{e}</code>)}
                  </div>}
            </Section>
          </>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="docker-drawer-section">
      <h4 className="docker-drawer-section-title">{title}</h4>
      {children}
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="docker-drawer-kv">
      <span className="docker-drawer-k" title={k}>{k}</span>
      <span className="docker-drawer-v" title={v}>{v}</span>
    </div>
  );
}
