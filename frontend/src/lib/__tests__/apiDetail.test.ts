import { describe, expect, it } from "vitest";
import { formatApiDetail } from "../apiDetail";

// Verbatim shape of a FastAPI 422 from POST /api/alerts/config (Task 5's handler).
const FASTAPI_422 = [
  { loc: ["to_email"], msg: "Field required", type: "missing" },
  { loc: ["thresholds", "cpu_crit"], msg: "Input should be less than or equal to 100",
    type: "less_than_equal" },
];

describe("formatApiDetail", () => {
  it("renders a plain-string detail unchanged", () => {
    expect(formatApiDetail("SMTP host not allowed", "fallback"))
      .toBe("SMTP host not allowed");
  });

  it("flattens a FastAPI validation array into loc: msg lines", () => {
    expect(formatApiDetail(FASTAPI_422, "fallback")).toBe(
      "to_email: Field required; thresholds.cpu_crit: Input should be less than or equal to 100",
    );
  });

  it("never returns an object, so it can always be rendered as a React child", () => {
    for (const input of [FASTAPI_422, { msg: "x" }, [{ nope: 1 }], null, undefined, 42]) {
      expect(typeof formatApiDetail(input, "fallback")).toBe("string");
    }
  });

  it("falls back rather than stringifying an unrecognised shape", () => {
    // JSON.stringify of a whole 422 could echo request values into the DOM.
    expect(formatApiDetail({ unexpected: "pw-under-test" }, "Save failed"))
      .toBe("Save failed");
    expect(formatApiDetail(undefined, "Save failed")).toBe("Save failed");
  });

  it("does not leak values from a malformed entry", () => {
    expect(formatApiDetail([{ loc: ["password"], input: "pw-under-test" }], "Save failed"))
      .toBe("Save failed");
  });
});
