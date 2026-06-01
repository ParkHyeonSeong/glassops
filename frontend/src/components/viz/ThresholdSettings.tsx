import { useEffect, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { useThresholdsStore } from "../../stores/thresholdsStore";
import { type MetricKey } from "../../lib/thresholds";

const ROWS: { key: MetricKey; label: string }[] = [
  { key: "cpu", label: "CPU" },
  { key: "mem", label: "Memory" },
  { key: "disk", label: "Disk" },
  { key: "core", label: "Per-core" },
];

export default function ThresholdSettings() {
  const [open, setOpen] = useState(false);
  const thresholds = useThresholdsStore((s) => s.thresholds);
  const setThreshold = useThresholdsStore((s) => s.setThreshold);
  const reset = useThresholdsStore((s) => s.reset);

  return (
    <div className="viz-thr">
      <button className="viz-thr-btn" onClick={() => setOpen((o) => !o)} title="Alert thresholds">
        <SlidersHorizontal size={14} />
      </button>
      {open && (
        <div className="viz-thr-panel">
          <div className="viz-thr-title">Alert thresholds (%)</div>
          <div className="viz-thr-grid">
            <span className="viz-thr-h" />
            <span className="viz-thr-h">Warn</span>
            <span className="viz-thr-h">Crit</span>
            {ROWS.map(({ key, label }) => (
              <Row key={key} label={label} warn={thresholds[key].warn} crit={thresholds[key].crit}
                onChange={(warn, crit) => setThreshold(key, { warn, crit })} />
            ))}
          </div>
          <button className="viz-thr-reset" onClick={reset}>Reset to defaults</button>
        </div>
      )}
    </div>
  );
}

function Row({ label, warn, crit, onChange }: {
  label: string; warn: number; crit: number;
  onChange: (warn: number, crit: number) => void;
}) {
  return (
    <>
      <span className="viz-thr-label">{label}</span>
      <NumField value={warn} onCommit={(v) => onChange(v, crit)} />
      <NumField value={crit} onCommit={(v) => onChange(warn, v)} />
    </>
  );
}

// Holds the raw text while typing and only commits (clamps) on blur/Enter, so
// per-keystroke warn/crit clamping can't swap the values mid-edit.
function NumField({ value, onCommit }: { value: number; onCommit: (v: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { setDraft(String(value)); }, [value]);

  const commit = () => {
    const n = Number(draft);
    if (draft.trim() === "" || Number.isNaN(n)) { setDraft(String(value)); return; }
    onCommit(Math.min(100, Math.max(0, n)));
  };

  return (
    <input
      className="viz-thr-input"
      type="number"
      min={0}
      max={100}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
    />
  );
}
