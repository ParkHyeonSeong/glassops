import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../stores/authStore";
import { useMetricsStore } from "../../../stores/metricsStore";
import { deferred, jsonResponse } from "../../../test/fixtures";
import { fetchWithAuth } from "../../../utils/api";
import SettingsApp from "../Settings";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

const LOADED = {
  configured: true,
  host: "relay.example.com",
  port: 587,
  username: "relay-login",
  password: "********",
  from_email: "alerts@example.com",
  to_email: "ops@example.com",
  security: "starttls",
  use_tls: false,
  start_tls: true,
  // Deliberately NOT the EMPTY_EMAIL_CONFIG defaults (90/90/95). If they matched, a
  // component that ignored d.thresholds entirely — the exact regression this suite
  // exists to catch — would still satisfy every assertion below.
  thresholds: { cpu_crit: 88, mem_crit: 71, disk_crit: 66 },
  password_decrypt_failed: false,
  security_ambiguous: false,
};

// Response-only fields the backend returns from GET but rejects on POST
// (SmtpConfig has extra="forbid"), so a UI that reposts the raw GET body 422s.
const RESPONSE_ONLY_FIELDS = ["configured", "password_decrypt_failed", "security_ambiguous"];

async function openEmailTab(config: Record<string, unknown> = LOADED) {
  vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse(config));
  render(<SettingsApp />);
  fireEvent.click(screen.getByRole("button", { name: "Email" }));
  await screen.findByLabelText("SMTP Host");
}

function bodyOf(call: number): Record<string, unknown> {
  const [, init] = vi.mocked(fetchWithAuth).mock.calls[call];
  return JSON.parse(String((init as RequestInit).body));
}

function methodOf(call: number): string | undefined {
  const [, init] = vi.mocked(fetchWithAuth).mock.calls[call];
  return (init as RequestInit | undefined)?.method;
}

describe("Settings > Email", () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
    useAuthStore.setState({ email: "admin@example.com", role: "admin" });
    useMetricsStore.setState({ agentId: null, connected: false });
  });

  // The integration acceptance: a full GET -> edit -> POST that returns 200, with
  // the response-only fields projected OUT of the POST body. Stock Settings.tsx
  // merged the whole GET response into state and reposted it, so configured /
  // password_decrypt_failed / security_ambiguous leaked and the strict router 422'd.
  it("round-trips GET -> edit -> POST 200 without leaking response-only fields", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.change(screen.getByLabelText("To Email (alerts)"),
      { target: { value: "oncall@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    const posted = bodyOf(1);
    for (const field of RESPONSE_ONLY_FIELDS) {
      expect(posted).not.toHaveProperty(field);
    }
    // Exact key set, not just "the known bad ones are absent": any future response
    // field merged into state would otherwise ride along unnoticed.
    expect(Object.keys(posted).sort()).toEqual([
      "clear_password", "from_email", "host", "password", "port", "security",
      "thresholds", "to_email", "username",
    ]);
    // The edit survived and the untouched password stayed masked.
    expect(posted.to_email).toBe("oncall@example.com");
    expect(posted.password).toBe("********");
    expect(methodOf(1)).toBe("POST");
    expect(methodOf(2)).toBe("POST");
  });

  it("drops legacy extra threshold keys the old dict schema allowed", async () => {
    // thresholds was `dict` before Task 5, so a stored row can carry arbitrary keys.
    // Re-posting them now fails EmailThresholds(extra="forbid") with a 422 the UI
    // gives the operator no way to clear.
    await openEmailTab({
      ...LOADED,
      thresholds: { cpu_crit: 88, mem_crit: 71, disk_crit: 66, gpu_crit: 80, legacy_key: 1 },
    });
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    expect(bodyOf(1).thresholds).toEqual({ cpu_crit: 88, mem_crit: 71, disk_crit: 66 });
  });

  it("keeps the form usable while the test send is in flight", async () => {
    // Only the SAVE request was previously held open, so a regression that cleared
    // pending during /test — allowing edits and duplicate sends — went unnoticed.
    await openEmailTab();
    const test = deferred<Response>();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockReturnValueOnce(test.promise);

    const button = screen.getByRole("button", { name: /Save & Send Test/i });
    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button", { name: /Sending/i })).toBeDisabled());
    expect(screen.getByLabelText("SMTP Host")).toBeDisabled();
    expect(screen.getByLabelText("Security")).toBeDisabled();
    expect(screen.getByLabelText("CPU critical (%)")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Sending/i }));
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3);  // load + save + the one test

    test.resolve(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));
  });

  it("reports an unknown outcome when the test response is lost", async () => {
    // The request may have reached the relay and only the response was lost, so
    // "failed" is wrong: it invites a retry that duplicates the test mail.
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockRejectedValueOnce(new Error("network down"));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    expect(await screen.findByText(/could not confirm the test result/i)).toBeInTheDocument();
    expect(screen.queryByText(/test send failed/i)).toBeNull();
    expect(screen.getByText(/network down/i)).toBeInTheDocument();
  });

  it("masks a newly entered password and clears the decrypt warning after saving", async () => {
    await openEmailTab({ ...LOADED, password: "", password_decrypt_failed: true });
    expect(screen.getByText(/could not be decrypted/i)).toBeInTheDocument();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw-under-test" } });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    expect(bodyOf(1).password).toBe("pw-under-test");   // the real one reached the API
    // ...but it must not linger in the DOM afterwards, and the stale warning is gone.
    await waitFor(() => expect(screen.getByLabelText("Password")).toHaveValue("********"));
    expect(screen.queryByText(/could not be decrypted/i)).toBeNull();
  });

  // "starttls" is also the EMPTY_EMAIL_CONFIG default, so asserting it alone would
  // pass against a component that ignored d.security. Tests 2 and 3 carry the mapping
  // coverage; this one earns its keep on the option list.
  it("offers all three security modes and defaults to STARTTLS", async () => {
    await openEmailTab();

    const select = screen.getByLabelText("Security");
    expect(select).toHaveValue("starttls");
    expect(Array.from((select as HTMLSelectElement).options).map((o) => o.value))
      .toEqual(["starttls", "implicit_tls", "none"]);
  });

  it("maps an implicit TLS config onto the security selector", async () => {
    await openEmailTab({ ...LOADED, security: "implicit_tls", use_tls: true, start_tls: false, port: 465 });

    expect(screen.getByLabelText("Security")).toHaveValue("implicit_tls");
  });

  it("maps a no-encryption config onto the security selector", async () => {
    await openEmailTab({ ...LOADED, security: "none", use_tls: false, start_tls: false, port: 25 });

    expect(screen.getByLabelText("Security")).toHaveValue("none");
  });

  it.each([
    ["starttls", 587],
    ["implicit_tls", 465],
    ["none", 25],
  ])("submits security=%s in the POST body", async (mode, port) => {
    // Pin the wire value, not just the rendered selection: the backend derives
    // use_tls/start_tls from this field alone.
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.change(screen.getByLabelText("Security"), { target: { value: mode } });
    fireEvent.change(screen.getByLabelText("Port"), { target: { value: String(port) } });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    expect(bodyOf(1).security).toBe(mode);
    expect(bodyOf(1).port).toBe(port);
  });

  it("shows the recommended port for the selected mode", async () => {
    await openEmailTab();

    fireEvent.change(screen.getByLabelText("Security"), { target: { value: "implicit_tls" } });

    expect(screen.getByText(/Recommended port: 465/)).toBeInTheDocument();
  });

  it("surfaces a config load failure instead of hanging on Loading", async () => {
    vi.mocked(fetchWithAuth).mockRejectedValueOnce(new Error("offline"));
    render(<SettingsApp />);
    fireEvent.click(screen.getByRole("button", { name: "Email" }));

    expect(await screen.findByText(/Could not load the email settings/i)).toBeInTheDocument();
    expect(screen.queryByText("Loading...")).toBeNull();
  });

  it("shows an error instead of an empty editable form on a non-OK load", async () => {
    // A 403/500 body is not a config. Rendering a blank form invites the operator to
    // "fix" it by saving, which would overwrite the real stored settings.
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ detail: "Admin access required" }, 403));
    render(<SettingsApp />);
    fireEvent.click(screen.getByRole("button", { name: "Email" }));

    expect(await screen.findByText(/Could not load the email settings/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("SMTP Host")).toBeNull();
    expect(screen.queryByRole("button", { name: /Save & Send Test/i })).toBeNull();
  });

  it("renders a FastAPI validation array without crashing the tab", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({
      detail: [{ loc: ["body", "to_email"], msg: "Field required", type: "missing" }],
    }, 422));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    // Not "Objects are not valid as a React child" — and the form survives.
    expect(await screen.findByText(/to_email: Field required/)).toBeInTheDocument();
    expect(screen.getByLabelText("SMTP Host")).toBeInTheDocument();
  });

  it("distinguishes a save that succeeded from a test that then failed", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ detail: "SMTP connection timed out" }, 400));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    expect(await screen.findByText(/Settings saved, but the test send failed/i))
      .toBeInTheDocument();
    expect(screen.getByText(/SMTP connection timed out/)).toBeInTheDocument();
  });

  it("disables the form while a request is in flight", async () => {
    await openEmailTab();
    const save = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValueOnce(save.promise);

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(screen.getByLabelText("SMTP Host")).toBeDisabled());
    expect(screen.getByLabelText("Security")).toBeDisabled();
    expect(screen.getByLabelText("CPU critical (%)")).toBeDisabled();

    save.resolve(jsonResponse({ ok: true }));
  });

  it("saves the current form and only then sends the test", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.change(screen.getByLabelText("To Email (alerts)"), {
      target: { value: "oncall@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    const [savePath] = vi.mocked(fetchWithAuth).mock.calls[1];
    const [testPath] = vi.mocked(fetchWithAuth).mock.calls[2];
    expect(savePath).toBe("/api/alerts/config");
    expect(testPath).toBe("/api/alerts/test");
    // The dirty value was saved before the test ran — not the stale DB config.
    expect(bodyOf(1).to_email).toBe("oncall@example.com");
  });

  it("does not send the test when saving fails, and shows the backend detail", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(
      jsonResponse({ detail: "SMTP port must be one of [25, 465, 587, 2525]" }, 400),
    );

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    expect(await screen.findByText(/SMTP port must be one of/)).toBeInTheDocument();
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(2); // load + failed save
  });

  it("blocks duplicate clicks while a request is in flight", async () => {
    await openEmailTab();
    const save = deferred<Response>();
    vi.mocked(fetchWithAuth).mockReturnValueOnce(save.promise);

    const button = screen.getByRole("button", { name: /Save & Send Test/i });
    fireEvent.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    fireEvent.click(button);
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(2); // load + the one save

    save.resolve(jsonResponse({ ok: true }));
  });

  it("clears the pending state when the network throws", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth).mockRejectedValueOnce(new Error("network down"));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    expect(await screen.findByText(/network down/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Save & Send Test/i })).toBeEnabled());
  });

  it("posts the mask back unchanged when the password is untouched", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    expect(bodyOf(1).password).toBe("********");
  });

  it("distinguishes SMTP acceptance from inbox delivery", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    const msg = await screen.findByText(/SMTP server accepted the message/i);
    expect(msg).toHaveTextContent(/check the inbox/i);
  });

  it("submits the email critical thresholds it loaded", async () => {
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    expect(screen.getByLabelText("CPU critical (%)")).toHaveValue(88);
    expect(screen.getByLabelText("Memory critical (%)")).toHaveValue(71);
    fireEvent.change(screen.getByLabelText("CPU critical (%)"), { target: { value: "77" } });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    // The untouched values must survive as the LOADED ones, not revert to defaults.
    expect(bodyOf(1).thresholds).toEqual({ cpu_crit: 77, mem_crit: 71, disk_crit: 66 });
  });

  it("refuses to guess an ambiguous legacy security mode", async () => {
    // Backend sends security: null for a row whose TLS flags contradict each other.
    // Defaulting to STARTTLS here would silently rewrite the operator's transport.
    await openEmailTab({ ...LOADED, security: null, security_ambiguous: true });

    expect(screen.getByLabelText("Security")).toHaveValue("");
    expect(screen.getByText(/Pick a mode before saving/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save & Send Test/i })).toBeDisabled();
    // security=null covers an unsupported mode and a non-boolean flag too, not only
    // contradictory flags — so the copy must not name one cause, and no port can be
    // recommended before a mode exists.
    expect(screen.queryByText(/flags contradict/i)).toBeNull();
    expect(screen.queryByText(/Recommended port/i)).toBeNull();
  });

  it("enables saving once an ambiguous mode is resolved, and sends the choice", async () => {
    await openEmailTab({ ...LOADED, security: null, security_ambiguous: true });
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, detail: "SMTP server accepted the message" }));

    fireEvent.change(screen.getByLabelText("Security"), { target: { value: "implicit_tls" } });
    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    await waitFor(() => expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(3));
    expect(bodyOf(1).security).toBe("implicit_tls");
  });

  it("still reports the save as landed when the test request itself throws", async () => {
    // A single outer catch would report a bare network error and lose the fact that
    // the configuration was already stored.
    await openEmailTab();
    vi.mocked(fetchWithAuth)
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockRejectedValueOnce(new Error("network down"));

    fireEvent.click(screen.getByRole("button", { name: /Save & Send Test/i }));

    expect(await screen.findByText(/could not confirm the test result/i)).toBeInTheDocument();
    expect(screen.getByText(/network down/i)).toBeInTheDocument();
  });

  it("shows an admin-only notice to a non-admin instead of the form", () => {
    useAuthStore.setState({ email: "user@example.com", role: "user" });
    render(<SettingsApp />);

    fireEvent.click(screen.getByRole("button", { name: "Email" }));

    expect(screen.getByText(/Admin access required/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("SMTP Host")).toBeNull();
    expect(vi.mocked(fetchWithAuth)).not.toHaveBeenCalled();
  });
});
