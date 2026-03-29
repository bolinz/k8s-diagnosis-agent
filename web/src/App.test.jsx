import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";

function okJson(payload) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(payload),
  });
}

describe("App", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: vi.fn(() => null),
        setItem: vi.fn(),
      },
      configurable: true,
    });
    window.history.replaceState({}, "", "/");
    cleanup();
  });

  it("renders empty list state when API returns no reports", async () => {
    vi.stubGlobal("fetch", vi.fn(() => okJson({ items: [] })));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByText("No reports match current filters.")).toBeInTheDocument();
    });
  });

  it("renders list error state when report list request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 503,
        }),
      ),
    );
    render(<App />);
    await waitFor(() => {
      expect(screen.getByText(/List fetch failed/i)).toBeInTheDocument();
    });
  });

  it("keeps detail visible when an old report detail request fails", async () => {
    const listPayload = {
      items: [
        {
          name: "legacy-r1",
          namespace: "default",
          severity: "critical",
          symptom: "ImagePullBackOff",
          summary: "legacy summary",
          workload: { kind: "Pod", name: "unknown" },
        },
      ],
    };
    const fetchMock = vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/legacy-r1")) {
          return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
        }
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/unknown" })).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText("legacy summary")).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/reports\/legacy-r1/),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("renders detail with key signals, top candidate and timeline emphasis", async () => {
    const listPayload = {
      items: [
        {
          name: "r1",
          namespace: "diag-e2e",
          severity: "critical",
          symptom: "FailedMount",
          summary: "PVC pending blocks scheduling",
          lastAnalyzedAt: "2026-03-28T02:23:45+00:00",
          triggerAt: "2026-03-28T02:20:00+00:00",
          source: "event",
          workload: { kind: "Pod", name: "checkout-abc" },
          relatedObjects: [{ kind: "Pod", namespace: "diag-e2e", name: "checkout-abc", role: "primary" }],
          rootCauseCandidates: [{ objectRef: { kind: "PersistentVolumeClaim", namespace: "diag-e2e", name: "checkout-pvc" } }],
        },
      ],
    };
    const detailPayload = {
      ...listPayload.items[0],
      recommendations: ["Check PVC binding"],
      probableCauses: ["PVC unbound"],
      evidence: ["FailedScheduling with unbound PVC"],
      rawSignal: {
        reason: "FailedMount",
        message: "Unable to attach or mount volumes",
        podPhase: "Pending",
      },
      relatedObjects: [
        { kind: "Pod", namespace: "diag-e2e", name: "checkout-abc", role: "primary" },
        { kind: "PersistentVolumeClaim", namespace: "diag-e2e", name: "checkout-pvc", role: "upstream-suspect" },
      ],
      rootCauseCandidates: [
        {
          objectRef: { kind: "PersistentVolumeClaim", namespace: "diag-e2e", name: "checkout-pvc" },
          reason: "PVC pending blocks mount",
          confidence: 0.89,
        },
      ],
      evidenceTimeline: [{ time: "2026-03-28T02:21:10+00:00", signal: "FailedMount" }],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/")) return okJson(detailPayload);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(window.navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const { container } = render(<App />);
    await waitFor(() => {
      expect(screen.getByText("Key Signals")).toBeInTheDocument();
    });
    expect(screen.getByText(/Event reason: FailedMount/i)).toBeInTheDocument();
    expect(screen.getByText("Top Root Candidate")).toBeInTheDocument();
    expect(screen.getByText("Root Cause Candidates")).toBeInTheDocument();
    expect(screen.getByLabelText("auto-refresh")).toBeChecked();
    expect(screen.getByRole("combobox", { name: "refresh-interval" })).toHaveValue("15");
    expect(screen.getByText("Observability")).toBeInTheDocument();
    expect(screen.queryByText("Requests")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "toggle-observability" }));
    expect(screen.getByText("Requests")).toBeInTheDocument();
    expect(screen.getByText("Status")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy Snapshot" }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Copied")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "all" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "overview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "attribution" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "timeline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "FailedMount (1)" })).toBeInTheDocument();
    expect(screen.getByText("Window")).toBeInTheDocument();
    expect(screen.getByText("Role")).toBeInTheDocument();
    expect(screen.getAllByText(/PVC pending blocks mount/i).length).toBeGreaterThan(0);
    expect(screen.getByText("1 related")).toBeInTheDocument();
    expect(screen.getByText("1 candidates")).toBeInTheDocument();
    expect(screen.getByText("First abnormal signal")).toBeInTheDocument();
    expect(container.querySelectorAll(".timeline-point-first").length).toBe(1);

    const points = container.querySelectorAll(".timeline-point");
    expect(points.length).toBeGreaterThan(0);
    fireEvent.click(points[0]);
    fireEvent.click(screen.getByRole("button", { name: /Show Event Navigator/i }));
    await waitFor(() => {
      expect(screen.getByText("Navigator Context")).toBeInTheDocument();
    });
    expect(screen.getByText("Timeline Inspector")).toBeInTheDocument();
    expect(screen.getAllByText("FailedMount").length).toBeGreaterThan(0);
    expect(screen.getByText(/Timeline focus:/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "timeline" }));
    expect(screen.queryByText("Root Cause Candidates")).not.toBeInTheDocument();
    expect(screen.getByText("Evidence Timeline")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Window"), { target: { value: "5m" } });
    expect(screen.getByLabelText("Window")).toHaveValue("5m");
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByLabelText("Window")).toHaveValue("all");
    fireEvent.click(screen.getByRole("button", { name: "all" }));
    expect(screen.getByText("Root Cause Candidates")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "owner" } });
    expect(screen.getByLabelText("Role")).toHaveValue("owner");
  });

  it("does not crash when switching between reports with and without timeline events", async () => {
    const listPayload = {
      items: [
        {
          name: "r-no-tl",
          namespace: "diag-e2e",
          severity: "critical",
          symptom: "Pending",
          summary: "no timeline report",
          workload: { kind: "Pod", name: "checkout-no-tl" },
        },
        {
          name: "r-with-tl",
          namespace: "diag-e2e",
          severity: "critical",
          symptom: "FailedMount",
          summary: "with timeline report",
          workload: { kind: "Pod", name: "checkout-with-tl" },
        },
      ],
    };
    const details = {
      "r-no-tl": {
        ...listPayload.items[0],
        evidenceTimeline: [],
      },
      "r-with-tl": {
        ...listPayload.items[1],
        evidenceTimeline: [
          { time: "2026-03-28T02:21:10+00:00", signal: "FailedMount" },
          { time: "2026-03-28T02:22:10+00:00", signal: "PVCPending" },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/r-no-tl")) return okJson(details["r-no-tl"]);
        if (url.includes("/api/reports/r-with-tl")) return okJson(details["r-with-tl"]);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );

    const { container } = render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/checkout-no-tl" })).toBeInTheDocument();
    });
    expect(screen.getByText("No timeline signals.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Pod\/checkout-with-tl/i }));
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/checkout-with-tl" })).toBeInTheDocument();
    });
    expect(container.querySelectorAll(".timeline-point").length).toBeGreaterThan(0);
  });

  it("hydrates filters/timezone/selected report from URL and keeps URL synced", async () => {
    window.history.replaceState({}, "", "/?tz=UTC&sym=failedmount&report=r2");
    const listPayload = {
      items: [
        {
          name: "r1",
          namespace: "diag-e2e",
          severity: "warning",
          symptom: "Pending",
          summary: "pending",
          workload: { kind: "Pod", name: "checkout-abc" },
        },
        {
          name: "r2",
          namespace: "diag-e2e",
          severity: "critical",
          symptom: "FailedMount",
          summary: "mount failed",
          workload: { kind: "Pod", name: "checkout-def" },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/r2")) return okJson(listPayload.items[1]);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );

    render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "timezone" })).toHaveValue("UTC");
    });
    expect(screen.getByRole("textbox", { name: "Symptom" })).toHaveValue("failedmount");
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/checkout-def" })).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole("combobox", { name: "timezone" }), { target: { value: "local" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Symptom" }), { target: { value: "" } });
    await waitFor(() => {
      expect(window.location.search).toContain("report=r2");
    });
    expect(window.location.search).not.toContain("tz=UTC");
    expect(window.location.search).not.toContain("sym=failedmount");
  });

  it("focuses symptom input with slash shortcut", async () => {
    vi.stubGlobal("fetch", vi.fn(() => okJson({ items: [] })));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByText("No reports match current filters.")).toBeInTheDocument();
    });
    fireEvent.keyDown(window, { key: "/" });
    expect(screen.getByRole("textbox", { name: "Symptom" })).toHaveFocus();
  });

  it("supports detail view and observability keyboard shortcuts", async () => {
    const listPayload = {
      items: [
        {
          name: "r1",
          namespace: "diag-e2e",
          severity: "warning",
          symptom: "Pending",
          summary: "pending",
          workload: { kind: "Pod", name: "checkout-a" },
        },
        {
          name: "r2",
          namespace: "diag-e2e",
          severity: "critical",
          symptom: "FailedMount",
          summary: "mount failed",
          workload: { kind: "Pod", name: "checkout-b" },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/r2")) return okJson(listPayload.items[1]);
        if (url.includes("/api/reports/r1")) return okJson(listPayload.items[0]);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );
    render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/checkout-a" })).toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: "Pod/checkout-a" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "1" });
    expect(screen.getByRole("button", { name: "all" })).toHaveClass("view-active");
    fireEvent.keyDown(window, { key: "4" });
    expect(screen.getByRole("button", { name: "timeline" })).toHaveClass("view-active");
    fireEvent.keyDown(window, { key: "o" });
    expect(screen.getByText("Requests")).toBeInTheDocument();
  });

  it("hydrates refresh and observability prefs from localStorage", async () => {
    const prefs = JSON.stringify({
      timezone: "UTC",
      autoRefreshEnabled: false,
      autoRefreshSeconds: 30,
      opsExpanded: true,
    });
    const setItem = vi.fn();
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: vi.fn((key) => (key === "k8s-diagnosis-ui-prefs-v1" ? prefs : null)),
        setItem,
      },
      configurable: true,
    });
    vi.stubGlobal("fetch", vi.fn(() => okJson({ items: [] })));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByText("No reports match current filters.")).toBeInTheDocument();
    });
    expect(screen.getByRole("combobox", { name: "timezone" })).toHaveValue("UTC");
    expect(screen.getByLabelText("auto-refresh")).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "refresh-interval" })).toHaveValue("30");
    expect(screen.getByText("Requests")).toBeInTheDocument();
    expect(setItem).toHaveBeenCalled();
  });

  it("disables interval control when auto refresh is off", async () => {
    vi.stubGlobal("fetch", vi.fn(() => okJson({ items: [] })));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByText("No reports match current filters.")).toBeInTheDocument();
    });
    const auto = screen.getByLabelText("auto-refresh");
    const interval = screen.getByRole("combobox", { name: "refresh-interval" });
    expect(interval).not.toBeDisabled();
    fireEvent.click(auto);
    expect(interval).toBeDisabled();
  });

  it("shows unified empty state in timeline view when no timeline data", async () => {
    const listPayload = {
      items: [
        {
          name: "r-empty",
          namespace: "diag-e2e",
          severity: "info",
          symptom: "Pending",
          summary: "pending",
          workload: { kind: "Pod", name: "empty-1" },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/")) return okJson(listPayload.items[0]);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );
    render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Pod/empty-1" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "timeline" }));
    expect(screen.getByText("No timeline data available for current time window.")).toBeInTheDocument();
  });

  it("highlights a timeline point after click even when objectRef is missing", async () => {
    const listPayload = {
      items: [
        {
          name: "r1",
          namespace: "diag-e2e",
          severity: "warning",
          symptom: "Pending",
          summary: "pending",
          workload: { kind: "Pod", name: "checkout-abc" },
        },
      ],
    };
    const detailPayload = {
      ...listPayload.items[0],
      evidenceTimeline: [
        { time: "2026-03-28T01:00:00+00:00", signal: "FailedScheduling" },
        { time: "2026-03-28T01:01:00+00:00", signal: "FailedMount" },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn((input) => {
        const url = String(input);
        if (url.includes("/api/reports/")) return okJson(detailPayload);
        if (url.includes("/api/reports")) return okJson(listPayload);
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
      }),
    );

    const { container } = render(<App />);
    await waitFor(() => {
      expect(screen.getByText("Evidence Timeline")).toBeInTheDocument();
    });
    expect(container.querySelectorAll(".timeline-point-active").length).toBe(0);

    const points = container.querySelectorAll(".timeline-point");
    expect(points.length).toBeGreaterThan(0);
    fireEvent.click(points[0]);

    await waitFor(() => {
      expect(container.querySelectorAll(".timeline-point-active").length).toBe(1);
    });
  });
});
