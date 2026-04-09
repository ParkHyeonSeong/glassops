import { AlertTriangle, Info, XCircle, X } from "lucide-react";
import { useAlertStore, type Alert } from "../../stores/alertStore";

const ICON_MAP = {
  info: Info,
  warning: AlertTriangle,
  error: XCircle,
};

function Toast({ alert }: { alert: Alert }) {
  const dismiss = useAlertStore((s) => s.dismiss);
  const Icon = ICON_MAP[alert.type];

  return (
    <div className={`toast toast-${alert.type}`}>
      <Icon size={15} className="toast-icon" />
      <span className="toast-message">{alert.message}</span>
      <button className="toast-close" onClick={() => dismiss(alert.id)} aria-label="Dismiss">
        <X size={12} />
      </button>
    </div>
  );
}

export default function ToastContainer() {
  const alerts = useAlertStore((s) => s.alerts);

  if (alerts.length === 0) return null;

  return (
    <div className="toast-container" role="log" aria-live="polite">
      {alerts.map((a) => (
        <Toast key={a.id} alert={a} />
      ))}
    </div>
  );
}
