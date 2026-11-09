import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
