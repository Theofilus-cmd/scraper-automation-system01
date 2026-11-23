import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AlertsPage from "../src/app/alerts/page";
import DeadLetterPage from "../src/app/dead-letter/page";
import DashboardPage from "../src/app/page";
import RunDetailPage from "../src/app/runs/[id]/page";
import SourceDetailPage from "../src/app/sources/[id]/page";
import { AppProviders } from "../src/components/app-shell";
import type { Source } from "../src/lib/types";

const push = vi.fn();
let routeParams: { id?: string } = {};

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useParams: () => routeParams,
  useRouter: () => ({ push }),
}));

const fetchMock = vi.fn();

type Routes = Record<string, unknown | (() => Response)>;

/** Route fetches by URL path (+ method) so tests don't depend on call order. */
function mockApi(routes: Routes) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const path = url.replace("http://localhost:8000", "");
    const key = `${init?.method ?? "GET"} ${path.split("?")[0]}`;
    const hit = routes[key];
    if (hit === undefined) {
      return { ok: false, status: 404, json: async () => ({ error: { message: `no mock: ${key}` } }) };
    }
    if (typeof hit === "function") return (hit as () => Response)();
    return { ok: true, status: 200, json: async () => hit };
  });
}

const page = <T,>(data: T[], next: string | null = null) => ({
  data,
  pagination: { next_cursor: next, has_more: next !== null },
});

const user = { id: "u1", email: "p@example.com", display_name: "Person", is_verified: true };
const baseRoutes: Routes = {
  "GET /readyz": { status: "ok" },
  "GET /api/v1/auth/me": user,
};

const source: Source = {
  id: "s1",
  url: "http://mock-store:4000/products/one",
  normalized_url: "http://mock-store:4000/products/one",
  adapter_type: "mock_store",
  status: "active",
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

function renderPage(node: React.ReactNode) {
  window.sessionStorage.setItem("scraper-automation.access-token", "tok");
  render(<AppProviders>{node}</AppProviders>);
}

describe("pages", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    fetchMock.mockReset();
    push.mockReset();
    routeParams = {};
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("dashboard shows an empty state", async () => {
    mockApi({
      ...baseRoutes,
      "GET /api/v1/sources": page([]),
      "GET /api/v1/alerts": page([]),
      "GET /api/v1/tasks": page([]),
    });
    renderPage(<DashboardPage />);
    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
  });

  it("dashboard shows an error with retry", async () => {
    mockApi({
      ...baseRoutes,
      "GET /api/v1/sources": () =>
        ({ ok: false, status: 500, json: async () => ({ error: { message: "Boom" } }) }) as Response,
      "GET /api/v1/alerts": page([]),
      "GET /api/v1/tasks": page([]),
    });
    renderPage(<DashboardPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Boom");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("dashboard deletes a source only after confirmation", async () => {
    mockApi({
      ...baseRoutes,
      "GET /api/v1/sources": page([source]),
      "GET /api/v1/alerts": page([]),
      "GET /api/v1/tasks": page([]),
      "DELETE /api/v1/sources/s1": () => ({ ok: true, status: 204, json: async () => ({}) }) as Response,
    });
    renderPage(<DashboardPage />);

    fireEvent.click(await screen.findByRole("button", { name: "Delete source" }));
    const dialog = screen.getByRole("dialog");
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/sources/s1"), expect.objectContaining({ method: "DELETE" }));

    // Cancel keeps the source.
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText(source.url)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete source" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByText(source.url)).not.toBeInTheDocument());
  });

  it("source detail renders price and stock charts and deletes via dialog", async () => {
    routeParams = { id: "s1" };
    const obs = (price: string, stock: string, at: string) => ({
      id: at, product_id: "p1", run_id: "r1", task_id: "t1", product_name: "W", brand: null,
      category: null, price, currency: "USD", original_price: null, stock_status: stock,
      rating: null, review_count: null, image_url: null, is_valid: true, scraped_at: at,
      version_created_at: at,
    });
    mockApi({
      ...baseRoutes,
      "GET /api/v1/sources/s1": {
        ...source,
        schedule: null,
        current_product: { product: { id: "p1", source_id: "s1" }, observation: obs("12.00", "in_stock", "2026-08-02T00:00:00Z") },
      },
      "GET /api/v1/products/p1/history": page([
        obs("12.00", "in_stock", "2026-08-02T00:00:00Z"),
        obs("10.00", "out_of_stock", "2026-08-01T00:00:00Z"),
      ]),
      "GET /api/v1/runs": page([]),
      "GET /api/v1/alerts": page([]),
      "DELETE /api/v1/sources/s1": () => ({ ok: true, status: 204, json: async () => ({}) }) as Response,
    });
    renderPage(<SourceDetailPage />);

    expect(await screen.findByRole("img", { name: /^Price: 2 observations.*latest 12 USD/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Stock status: 2 observations.*latest In stock/ })).toBeInTheDocument();
    expect(screen.getByText("No runs yet")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete source" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"));
  });

  it("source detail shows empty history", async () => {
    routeParams = { id: "s1" };
    mockApi({
      ...baseRoutes,
      "GET /api/v1/sources/s1": { ...source, schedule: null, current_product: null },
      "GET /api/v1/runs": page([]),
      "GET /api/v1/alerts": page([]),
    });
    renderPage(<SourceDetailPage />);
    expect(await screen.findByText("No history yet")).toBeInTheDocument();
  });

  it("run detail lists tasks", async () => {
    routeParams = { id: "run-12345678" };
    mockApi({
      ...baseRoutes,
      "GET /api/v1/runs/run-12345678": {
        id: "run-12345678", source_id: "s1", schedule_id: null, status: "failed",
        triggered_by: "manual", total_tasks: 1, succeeded_tasks: 0, failed_tasks: 1,
        started_at: null, finished_at: null, created_at: "2026-08-29T00:00:00Z",
      },
      "GET /api/v1/runs/run-12345678/tasks": page([
        { id: "t1", run_id: "run-12345678", source_id: "s1", status: "dead_letter", attempt_count: 3,
          max_attempts: 3, error_reason: "http_5xx", error_detail: null, queued_at: null, started_at: null, finished_at: null },
      ]),
    });
    renderPage(<RunDetailPage />);
    expect(await screen.findByText("http_5xx")).toBeInTheDocument();
    expect(screen.getByText("3/3")).toBeInTheDocument();
  });

  it("dead letter page shows empty state and paginates", async () => {
    mockApi({ ...baseRoutes, "GET /api/v1/tasks": page([]) });
    renderPage(<DeadLetterPage />);
    expect(await screen.findByText("No dead-lettered tasks")).toBeInTheDocument();
  });

  it("alerts page lists alerts and loads more", async () => {
    const alert = (id: string, name: string) => ({
      id, source_id: "s1", product_id: "p1", run_id: null, task_id: null, kind: "price_changed",
      product_name: name, product_url: "u", currency: "USD", old_value: "1", new_value: "2",
      email_status: "sent", sent_at: null, created_at: "2026-08-29T00:00:00Z",
    });
    let calls = 0;
    fetchMock.mockImplementation(async (url: string) => {
      const path = url.replace("http://localhost:8000", "");
      if (path === "/readyz") return { ok: true, status: 200, json: async () => ({}) };
      if (path === "/api/v1/auth/me") return { ok: true, status: 200, json: async () => user };
      calls += 1;
      const body = url.includes("cursor=c2") ? page([alert("a2", "Second")]) : page([alert("a1", "First")], "c2");
      return { ok: true, status: 200, json: async () => body };
    });
    renderPage(<AlertsPage />);
    expect(await screen.findByText("First")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Second")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(calls).toBe(2);
  });
});
