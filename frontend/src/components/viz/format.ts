export type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";

export function formatTime(ts: number, range: TimeRange): string {
  const d = new Date(ts * 1000);
  if (range === "live" || range === "5m" || range === "1h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  if (range === "6h" || range === "24h") {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

// Network rates arrive as bytes/sec. Auto-scale the unit so small (idle) rates
// stay visible instead of rounding to "0.0 MB/s".
export function formatRate(bytesPerSec: number): string {
  if (!bytesPerSec || bytesPerSec < 1) return "0 B/s";
  const k = 1024;
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytesPerSec) / Math.log(k)));
  return `${(bytesPerSec / Math.pow(k, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}
