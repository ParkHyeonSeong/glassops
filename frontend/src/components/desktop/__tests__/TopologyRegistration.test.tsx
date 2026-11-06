import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../../stores/authStore";
import { useWindowStore } from "../../../stores/windowStore";
import Dock from "../Dock";
import MobileDesktop from "../MobileDesktop";
import WindowManager from "../WindowManager";

// Importing the Terminal app probes a canvas at load; jsdom has none and would print a warning.
vi.hoisted(() => { HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext; });

vi.mock("../../../hooks/useActiveAlerts", () => ({ useActiveAlerts: vi.fn(() => []) }));

describe("Topology registration", () => {
  beforeEach(() => {
    // The app polls /api/time for its clock; it never answers here.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    useWindowStore.setState({ windows: [], nextZIndex: 1, lastSizes: {} });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("opens Topology for a non-admin", () => {
    useAuthStore.setState({ role: "user" });
    act(() => useWindowStore.getState().openWindow("topology"));
    render(<WindowManager />);
    expect(document.querySelector(".topology-canvas")).not.toBeNull();
  });

  it("lists Topology on mobile", () => {
    useAuthStore.setState({ role: "user" });
    render(<MobileDesktop connectionStatus="connected" />);
    const icon = screen.getByRole("button", { name: "Topology" });
    // The fallback icon is an svg too, so check for the Network glyph itself.
    expect(icon.querySelector("svg.lucide-network")).not.toBeNull();
    fireEvent.click(icon);
    expect(document.querySelector(".topology-canvas")).not.toBeNull();
  });

  it("shows Topology in the dock", () => {
    useAuthStore.setState({ role: "user" });
    render(<Dock />);
    // The fallback icon is an svg too, so check for the Network glyph itself.
    expect(screen.getByRole("button", { name: "Topology" }).querySelector("svg.lucide-network")).not.toBeNull();
  });
});
