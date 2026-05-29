// Single source for chart/series colors, semantic metric colors, severity colors,
// and the shared Recharts tooltip style. Values match the existing design tokens.
export const SERIES_COLORS = [
  "#4facfe", "#43e97b", "#f7971e", "#a18cd1", "#f85032",
  "#38f9d7", "#fccb90", "#667eea",
];

export const CORE_COLORS = [
  "#4facfe", "#43e97b", "#f7971e", "#f85032", "#a18cd1",
  "#38f9d7", "#fccb90", "#e0c3fc", "#667eea", "#764ba2",
  "#63e6be", "#ffa94d", "#ff6b6b", "#da77f2", "#20c997", "#fab005",
];

export const METRIC_COLORS = {
  cpu: "#4facfe", mem: "#43e97b", disk: "#f7971e", net: "#4facfe", gpu: "#a18cd1",
} as const;

export const SEVERITY_COLORS = {
  ok: "#43e97b", warn: "#f7971e", crit: "#f85032",
} as const;

export const TOOLTIP_STYLE = {
  background: "rgba(20,20,40,0.9)",
  border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8,
  fontSize: 11,
  color: "#e0e0e0",
} as const;
