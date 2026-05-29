import { useState } from "react";
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
      <input className="viz-thr-input" type="number" min={0} max={100} value={warn}
        onChange={(e) => onChange(Number(e.target.value), crit)} />
      <input className="viz-thr-input" type="number" min={0} max={100} value={crit}
        onChange={(e) => onChange(warn, Number(e.target.value))} />
    </>
  );
}
