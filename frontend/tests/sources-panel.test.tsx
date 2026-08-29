import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourcesPanel } from "../src/components/sources-panel";
import { ApiError, createSource, listSources } from "../src/lib/api";
import type { Source } from "../src/lib/types";

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();

  return {
    ...actual,
    createSource: vi.fn(),
    listSources: vi.fn(),
  };
});

const mockedCreateSource = vi.mocked(createSource);
const mockedListSources = vi.mocked(listSources);

const existingSource: Source = {
  id: "source-existing",
  url: "http://mock-store:4000/products/existing",
  normalized_url: "http://mock-store:4000/products/existing",
  adapter_type: "mock_store",
  status: "active",
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

const addedSource: Source = {
  id: "source-added",
  url: "http://mock-store:4000/products/added",
  normalized_url: "http://mock-store:4000/products/added",
  adapter_type: "mock_store",
  status: "paused",
  created_at: "2026-08-29T00:01:00Z",
  updated_at: "2026-08-29T00:01:00Z",
};

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.resetAllMocks();
});

describe("SourcesPanel", () => {
  it("loads and renders workspace sources", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText(existingSource.url)).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(mockedListSources).toHaveBeenCalledWith("test-token");
  });

  it("shows an empty state when the workspace has no sources", async () => {
    mockedListSources.mockResolvedValue({
      data: [],
      pagination: { next_cursor: null, has_more: false },
    });

    render(<SourcesPanel token="test-token" />);

    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
  });

  it("adds a source and clears the URL field", async () => {
    mockedListSources.mockResolvedValue({
      data: [existingSource],
      pagination: { next_cursor: null, has_more: false },
    });
    mockedCreateSource.mockResolvedValue(addedSource);

    render(<SourcesPanel token="test-token" />);

    await screen.findByText(existingSource.url);

    const urlInput = screen.getByLabelText("Product URL");
    fireEvent.change(urlInput, {
      target: { value: `  ${addedSource.url}  ` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));

    await waitFor(() => {
      expect(mockedCreateSource).toHaveBeenCalledWith({
        token: "test-token",
        url: addedSource.url,
      });
    });

    expect(await screen.findByText(addedSource.url)).toBeInTheDocument();
    expect(urlInput).toHaveValue("");
  });

  it("shows an API error when loading sources fails", async () => {
    mockedListSources.mockRejectedValue(new ApiError("Unauthorized", 401));

    render(<SourcesPanel token="invalid-token" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Unauthorized");
  });
});
