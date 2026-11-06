// Icon drawings for the Topology map that lucide does not have. Both are original
// drawings from the design mockup; the whale is deliberately not the Docker logo.

interface GlyphProps {
  size?: number;
}

export function WhaleGlyph({ size = 31 }: GlyphProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <g fill="currentColor">
        <rect x="5.5" y="11" width="4.2" height="4" rx=".7" />
        <rect x="10.3" y="11" width="4.2" height="4" rx=".7" />
        <rect x="15.1" y="11" width="4.2" height="4" rx=".7" />
        <rect x="19.9" y="11" width="4.2" height="4" rx=".7" />
        <rect x="10.3" y="6.4" width="4.2" height="4" rx=".7" />
        <rect x="15.1" y="6.4" width="4.2" height="4" rx=".7" />
        <path d="M2.4 16.4h22.4c1.3 0 2.3-.8 2.9-1.9.7.4 1.6.6 2.5.5-.3 1.7-1.5 2.8-2.8 3.3C25.8 23 21 26.8 14.3 26.8 7.6 26.8 3.4 22.6 2.4 16.4Z" />
      </g>
      <circle cx="8.6" cy="20" r="1.05" fill="#10142a" />
    </svg>
  );
}

export function GpuGlyph({ size = 26 }: GlyphProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="2" y="6" width="17" height="11" rx="2" />
      <circle cx="7.5" cy="11.5" r="2.7" />
      <circle cx="13.5" cy="11.5" r="2.7" />
      <path d="M19 8.5h2.5v6H19M5 17v2.5M8.5 17v2.5M12 17v2.5" />
    </svg>
  );
}
