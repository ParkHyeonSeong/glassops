import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deferred, jsonResponse } from "../../../test/fixtures";
import { fetchWithAuth } from "../../../utils/api";
import LogViewer from "../LogViewer";

vi.mock("../../../utils/api", () => ({ fetchWithAuth: vi.fn() }));

describe("LogViewer", () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
  });

  it("keeps the newest search when an older request resolves last", async () => {
    const initial = deferred<Response>();
    const searched = deferred<Response>();
    vi.mocked(fetchWithAuth).mockImplementation((path) => {
      if (path === "/api/logs/sources") {
        return Promise.resolve(jsonResponse({
          sources: [{ type: "system", name: "syslog" }],
        }));
      }
      const query = new URL(path, "http://glassops.local").searchParams;
      return query.get("search") === "new" ? searched.promise : initial.promise;
    });

    render(<LogViewer />);
    const input = await screen.findByPlaceholderText("Search...");
    await waitFor(() => {
      const initialReadStarted = vi.mocked(fetchWithAuth).mock.calls.some(([path]) => (
        path.startsWith("/api/logs/read?")
        && new URL(path, "http://glassops.local").searchParams.get("search") === ""
      ));
      expect(initialReadStarted).toBe(true);
    });
    fireEvent.change(input, { target: { value: "new" } });

    await act(async () => {
      searched.resolve(jsonResponse({ lines: ["new result"] }));
      await searched.promise;
    });
    expect(screen.getByText("new result")).toBeInTheDocument();

    await act(async () => {
      initial.resolve(jsonResponse({ lines: ["old result"] }));
      await initial.promise;
    });
    expect(screen.getByText("new result")).toBeInTheDocument();
    expect(screen.queryByText("old result")).not.toBeInTheDocument();
  });

  it("does not reuse a result after leaving and returning to a search", async () => {
    const firstA = deferred<Response>();
    const middleB = deferred<Response>();
    const secondA = deferred<Response>();
    let emptySearchReads = 0;
    vi.mocked(fetchWithAuth).mockImplementation((path) => {
      if (path === "/api/logs/sources") {
        return Promise.resolve(jsonResponse({
          sources: [{ type: "system", name: "syslog" }],
        }));
      }
      const query = new URL(path, "http://glassops.local").searchParams;
      if (query.get("search") === "b") return middleB.promise;
      emptySearchReads += 1;
      return emptySearchReads === 1 ? firstA.promise : secondA.promise;
    });

    render(<LogViewer />);
    const input = await screen.findByPlaceholderText("Search...");
    await waitFor(() => expect(emptySearchReads).toBe(1));

    await act(async () => {
      firstA.resolve(jsonResponse({ lines: ["first A result"] }));
      await firstA.promise;
    });
    expect(await screen.findByText("first A result")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "b" } });
    await waitFor(() => {
      const middleStarted = vi.mocked(fetchWithAuth).mock.calls.some(([path]) => (
        path.startsWith("/api/logs/read?")
        && new URL(path, "http://glassops.local").searchParams.get("search") === "b"
      ));
      expect(middleStarted).toBe(true);
    });

    fireEvent.change(input, { target: { value: "" } });
    await waitFor(() => expect(emptySearchReads).toBe(2));
    expect(screen.queryByText("first A result")).not.toBeInTheDocument();
    expect(screen.getByTitle("Refresh").querySelector("svg")
      ?.classList.contains("log-spin") ?? false).toBe(true);

    await act(async () => {
      secondA.resolve(jsonResponse({ lines: ["second A result"] }));
      await secondA.promise;
    });
    expect(await screen.findByText("second A result")).toBeInTheDocument();
  });

  it("refreshes the active query every five seconds and shows loading", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      const polled = deferred<Response>();
      let reads = 0;
      vi.mocked(fetchWithAuth).mockImplementation((path) => {
        if (path === "/api/logs/sources") {
          return Promise.resolve(jsonResponse({
            sources: [{ type: "system", name: "syslog" }],
          }));
        }
        reads += 1;
        return reads === 1
          ? Promise.resolve(jsonResponse({ lines: ["current"] }))
          : polled.promise;
      });

      render(<LogViewer />);
      const readCount = () => vi.mocked(fetchWithAuth).mock.calls
        .filter(([path]) => path.startsWith("/api/logs/read?")).length;
      const refreshIsSpinning = () => screen.getByTitle("Refresh")
        .querySelector("svg")?.classList.contains("log-spin") ?? false;

      await waitFor(() => expect(readCount()).toBe(1));
      await waitFor(() => expect(refreshIsSpinning()).toBe(false));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      await waitFor(() => expect(readCount()).toBe(2));
      expect(refreshIsSpinning()).toBe(true);

      await act(async () => {
        polled.resolve(jsonResponse({ lines: ["polled"] }));
        await polled.promise;
      });
      await waitFor(() => expect(refreshIsSpinning()).toBe(false));
    } finally {
      vi.useRealTimers();
    }
  });
});
