import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../../../test/fixtures";
import { fetchWithAuth } from "../../../utils/api";
import SettingsApp from "../Settings";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

async function openServer(gpuAvailable = true, enabled = "false") {
  vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({
    config: { enable_gpu: enabled, enable_docker: "true", collect_interval: "1" },
    gpu_available: gpuAvailable,
  }));
  render(<SettingsApp />);
  fireEvent.click(screen.getByRole("button", { name: "Server" }));
  return screen.findByRole("switch", { name: "GPU Monitoring" });
}

describe("Settings > Server GPU monitoring", () => {
  beforeEach(() => vi.mocked(fetchWithAuth).mockReset());

  it("saves only the changed GPU flag and explains application without a restart", async () => {
    const toggle = await openServer();
    fireEvent.click(toggle);
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ ok: true, updated: ["enable_gpu"] }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/GPU monitoring.*next collection cycle/i);
    const [url, init] = vi.mocked(fetchWithAuth).mock.calls[1];
    expect(url).toBe("/api/settings/runtime");
    expect(JSON.parse(String(init?.body))).toEqual({ enable_gpu: "true" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(2);
  });

  it("explains missing device access and blocks turning an unavailable GPU on", async () => {
    const toggle = await openServer(false);
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/make up-gpu/)).toBeVisible();
    expect(screen.getByText(/dashboard host/i)).toBeVisible();
  });

  it("allows a stored ON setting to be turned off even when GPU access is missing", async () => {
    const toggle = await openServer(false, "true");
    expect(toggle).toBeEnabled();
    fireEvent.click(toggle);
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ ok: true, updated: ["enable_gpu"] }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeDisabled());
    expect(JSON.parse(String(vi.mocked(fetchWithAuth).mock.calls[1][1]?.body))).toEqual({ enable_gpu: "false" });
  });

  it("keeps edits retryable and shows the API guidance when GPU access disappears", async () => {
    const toggle = await openServer();
    fireEvent.click(toggle);
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ detail: "GPU access unavailable. Recreate with make up-gpu." }, 409));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/Recreate with make up-gpu/);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});

const runtime = (draft = "10.0.0.0/8", pending = true, hasDraft = true, ready = true) => ({
  config: { enable_gpu: "false", enable_docker: "true", collect_interval: "1", terminal_user: "", allowed_ips: draft },
  gpu_available: true,
  ip_rules: { active: ready ? (pending ? "" : "10.0.0.0/8") : null, pending, has_saved_draft: hasDraft, client_ip: "10.2.3.4", nginx_ready: ready },
});

async function openRuntime(data = runtime()) {
  vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse(data));
  const view = render(<SettingsApp />);
  fireEvent.click(screen.getByRole("button", { name: "Server" }));
  await screen.findByRole("switch", { name: "Docker Monitoring" });
  return view;
}

describe("Settings > Server application flow", () => {
  beforeEach(() => vi.mocked(fetchWithAuth).mockReset());

  it("saves mixed settings with separate application guidance and enables a newly saved IP draft", async () => {
    await openRuntime(runtime("", false, false));
    expect(screen.queryByRole("button", { name: /Restart Agent/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply IP Rules" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Docker Monitoring" }));
    fireEvent.change(screen.getByLabelText(/Collection Interval/), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("Terminal User"), { target: { value: "ubuntu" } });
    fireEvent.change(screen.getByLabelText(/IP Whitelist/), { target: { value: "10.0.0.0/8" } });
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse(runtime()));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/Docker monitoring.*next collection cycle/i);
    expect(screen.getByText(/collection interval.*current wait/i)).toBeVisible();
    expect(screen.getByText(/terminal user.*new local terminal/i)).toBeVisible();
    expect(screen.getAllByText(/IP draft.*not applied/i).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Apply IP Rules" })).toBeEnabled();
    expect(JSON.parse(String(vi.mocked(fetchWithAuth).mock.calls[1][1]?.body))).toEqual({
      enable_docker: "false", collect_interval: "5", terminal_user: "ubuntu", allowed_ips: "10.0.0.0/8",
    });
    expect(vi.mocked(fetchWithAuth).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("requires saving IP edits before showing a confirmation and cancel never applies", async () => {
    await openRuntime();
    fireEvent.change(screen.getByLabelText(/IP Whitelist/), { target: { value: "10.2.3.4" } });
    expect(screen.getByRole("button", { name: "Apply IP Rules" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/IP Whitelist/), { target: { value: "10.0.0.0/8" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply IP Rules" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("10.2.3.4");
    expect(screen.getByRole("dialog")).toHaveTextContent(/terminal sessions will end/i);
    expect(screen.getByRole("dialog")).toHaveTextContent(/remote agents/i);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(1);
  });

  it.each(["network", "502", "503", "504"])("verifies actual state after %s without repeating Apply", async (failure) => {
    await openRuntime();
    fireEvent.click(screen.getByRole("button", { name: "Apply IP Rules" }));
    if (failure === "network") vi.mocked(fetchWithAuth).mockRejectedValueOnce(new TypeError("Disconnected"));
    else vi.mocked(fetchWithAuth).mockResolvedValueOnce(new Response("proxy unavailable", { status: Number(failure) }));
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse(runtime("10.0.0.0/8", false)));
    fireEvent.click(screen.getByRole("button", { name: "Confirm and apply" }));
    await screen.findByText(/IP rules applied and verified/i);
    const posts = vi.mocked(fetchWithAuth).mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe("/api/settings/ip-rules/apply");
    expect(JSON.parse(String(posts[0][1]?.body))).toEqual({ allowed_ips: "10.0.0.0/8" });
  });

  it("keeps a definite validation failure visible without retrying", async () => {
    await openRuntime();
    fireEvent.click(screen.getByRole("button", { name: "Apply IP Rules" }));
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ detail: "Your IP would be blocked." }, 400));
    fireEvent.click(screen.getByRole("button", { name: "Confirm and apply" }));
    await screen.findByText("Your IP would be blocked.");
    expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(2);
  });

  it("ends bounded verification honestly when nginx has not started", async () => {
    await openRuntime();
    fireEvent.click(screen.getByRole("button", { name: "Apply IP Rules" }));
    vi.useFakeTimers();
    try {
      vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ outcome: "unknown" }, 202));
      vi.mocked(fetchWithAuth).mockImplementation(async () => jsonResponse(runtime("10.0.0.0/8", true, true, false)));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm and apply" })));
      await act(async () => vi.advanceTimersByTimeAsync(16000));
      expect(screen.getByText(/Apply status needs verification/i)).toBeVisible();
      expect(screen.queryByText(/IP rules applied and verified/i)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Refresh IP status" })).toBeEnabled();
      expect(vi.mocked(fetchWithAuth).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops status polling when the Server tab is closed", async () => {
    const view = await openRuntime();
    fireEvent.click(screen.getByRole("button", { name: "Apply IP Rules" }));
    vi.useFakeTimers();
    try {
      vi.mocked(fetchWithAuth).mockResolvedValueOnce(jsonResponse({ outcome: "unknown" }, 202));
      vi.mocked(fetchWithAuth).mockImplementation(async () => jsonResponse(runtime("10.0.0.0/8", true, true, false)));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm and apply" })));
      const calls = vi.mocked(fetchWithAuth).mock.calls.length;
      view.unmount();
      await act(async () => vi.advanceTimersByTimeAsync(16000));
      expect(vi.mocked(fetchWithAuth)).toHaveBeenCalledTimes(calls);
    } finally {
      vi.useRealTimers();
    }
  });
});
