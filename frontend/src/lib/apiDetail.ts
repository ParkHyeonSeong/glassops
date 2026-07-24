interface ValidationEntry {
  loc: (string | number)[];
  msg: string;
}

function isValidationEntry(v: unknown): v is ValidationEntry {
  if (typeof v !== "object" || v === null) return false;
  const e = v as Record<string, unknown>;
  return Array.isArray(e.loc) && typeof e.msg === "string";
}

/**
 * Turn an API `detail` into something safe to render.
 *
 * FastAPI answers a validation failure with an ARRAY of objects, while the same
 * route's 400s carry a plain string. Rendering the array directly throws
 * "Objects are not valid as a React child" and blanks the tab, and TypeScript
 * cannot catch it because `res.json()` is `any`.
 *
 * Only `loc` and `msg` are ever read. An unrecognised shape falls back rather than
 * being stringified, because a raw dump could echo submitted values — including a
 * password — into the DOM.
 */
export function formatApiDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    const lines = detail.filter(isValidationEntry).map((e) => {
      // Drop FastAPI's leading "body" segment; it means nothing to an operator.
      const path = e.loc.map(String).filter((p) => p !== "body").join(".");
      return path ? `${path}: ${e.msg}` : e.msg;
    });
    if (lines.length) return lines.join("; ");
  }

  return fallback;
}
