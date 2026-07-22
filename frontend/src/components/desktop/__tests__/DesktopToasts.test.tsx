import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAlertStore } from "../../../stores/alertStore";
import { useMetricsStore } from "../../../stores/metricsStore";
import { useThresholdsStore } from "../../../stores/thresholdsStore";
import { DEFAULT_THRESHOLDS } from "../../../lib/thresholds";
import { makeMetricSnapshot } from "../../../test/fixtures";
import Desktop from "../Desktop";

// Desktop pulls in the websocket/keyboard hooks and the whole window manager;
// stub the parts that are irrelevant to threshold->toast wiring.
vi.mock("../../../hooks/useWebSocket", () => ({ useWebSocket: () => {} }));
vi.mock("../../../hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: () => {} }));
vi.mock("../../../hooks/useIsMobile", () => ({ useIsMobile: () => true }));
vi.mock("../MobileDesktop", () => ({ default: () => <div /> }));
vi.mock("../../common/ToastContainer", () => ({ default: () => <div /> }));

describe("Desktop threshold toasts", () => {
  beforeEach(() => {
    useThresholdsStore.setState({ thresholds: DEFAULT_THRESHOLDS, muted: {} });
    useMetricsStore.setState({ connected: true, current: null });
  });

  it("toasts from the shared thresholds store at the exact boundary", () => {
    const push = vi.fn();
    useAlertStore.setState({ push });
    // 95 is thresholdsStore's CPU crit. The retired settingsStore used 90, so a
    // component still reading the old store would emit a different severity here.
    useMetricsStore.setState({
      current: makeMetricSnapshot({
        cpu: { percent_total: 95, percent_per_core: [95], count_logical: 1,
               count_physical: 1, freq_current: 1000, freq_max: 2000 },
      }),
    });

    render(<Desktop />);

    expect(push).toHaveBeenCalledWith("error", expect.stringContaining("CPU"), "cpu-crit");
  });

  it("does not toast per-core spikes", () => {
    const push = vi.fn();
    useAlertStore.setState({ push });
    useMetricsStore.setState({
      current: makeMetricSnapshot({
        cpu: { percent_total: 20, percent_per_core: Array(32).fill(100),
               count_logical: 32, count_physical: 16, freq_current: 1000, freq_max: 2000 },
      }),
    });

    render(<Desktop />);

    expect(push).not.toHaveBeenCalled();
  });
});
