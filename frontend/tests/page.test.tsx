import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "../src/app/page";

describe("HomePage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: true } as Response)),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the heading and an api status indicator", async () => {
    render(<HomePage />);

    expect(screen.getByText("Scraper Automation System")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("api-status")).toHaveTextContent("ready");
    });
  });
});
