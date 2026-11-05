import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useWindowStore } from "../../stores/windowStore";
import { useKeyboardShortcuts } from "../useKeyboardShortcuts";

function Harness() {
  useKeyboardShortcuts();
  return <><div data-dock-menu><button>inside</button></div><button>outside</button></>;
}

describe("useKeyboardShortcuts Escape", () => {
  it("does not close the focused window when Escape comes from the dock menu", () => {
    const closeFocusedWindow = vi.fn();
    useWindowStore.setState({ closeFocusedWindow });
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole("button", { name: "inside" }), { key: "Escape" });
    expect(closeFocusedWindow).not.toHaveBeenCalled();
  });
  it("still closes the focused window for Escape elsewhere", () => {
    const closeFocusedWindow = vi.fn();
    useWindowStore.setState({ closeFocusedWindow });
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole("button", { name: "outside" }), { key: "Escape" });
    expect(closeFocusedWindow).toHaveBeenCalledOnce();
  });
  it("still closes the focused window for Escape that does not come from an element", () => {
    const closeFocusedWindow = vi.fn();
    useWindowStore.setState({ closeFocusedWindow });
    render(<Harness />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(closeFocusedWindow).toHaveBeenCalledOnce();
  });
});

describe("useKeyboardShortcuts Cmd+W", () => {
  it("still closes the focused window from inside the dock menu", () => {
    const closeFocusedWindow = vi.fn();
    useWindowStore.setState({ closeFocusedWindow });
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole("button", { name: "inside" }), { key: "w", metaKey: true });
    expect(closeFocusedWindow).toHaveBeenCalledOnce();
  });
});
