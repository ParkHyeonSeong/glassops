import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../stores/authStore";
import { useMetricsStore } from "../../../stores/metricsStore";
import { useThresholdsStore } from "../../../stores/thresholdsStore";
import { DEFAULT_THRESHOLDS } from "../../../lib/thresholds";
import SettingsApp from "../Settings";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

function openAlertsTab() {
  render(<SettingsApp />);
  fireEvent.click(screen.getByRole("button", { name: "Alerts" }));
}

describe("Settings > Alerts", () => {
  beforeEach(() => {
    useThresholdsStore.setState({ thresholds: DEFAULT_THRESHOLDS, muted: {} });
    useAuthStore.setState({ email: "admin@example.com", role: "admin" });
    useMetricsStore.setState({ agentId: null, connected: false });
  });

  it("renders values from the shared thresholds store, not the legacy defaults", () => {
    useThresholdsStore.setState({
      thresholds: { ...DEFAULT_THRESHOLDS, cpu: { warn: 63, crit: 91 } },
    });

    openAlertsTab();

    // 63 is only reachable from useThresholdsStore; the retired settingsStore
    // default for CPU Warning was 70.
    expect(screen.getByLabelText("CPU Warning")).toHaveValue("63");
    expect(screen.getByLabelText("CPU Critical")).toHaveValue("91");
  });

  it("writes edits back to the shared thresholds store", () => {
    openAlertsTab();

    fireEvent.change(screen.getByLabelText("Memory Warning"), { target: { value: "55" } });

    expect(useThresholdsStore.getState().thresholds.mem.warn).toBe(55);
  });

  it("does not expose a per-core alert threshold", () => {
    openAlertsTab();

    expect(screen.queryByLabelText(/per-core/i)).toBeNull();
    expect(screen.getByText(/Email alerts use their own server-side thresholds/i))
      .toBeInTheDocument();
  });
});
