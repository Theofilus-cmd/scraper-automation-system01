import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "../src/app/page";

const fetchMock = vi.fn();

describe("HomePage", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders readiness and the sign-in form", async () => {
    fetchMock.mockResolvedValueOnce({
      json: async () => ({ status: "ok" }),
      ok: true,
    } as Response);

    render(<HomePage />);

    expect(screen.getByRole("heading", { name: "Scraper Automation System" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("api-status")).toHaveTextContent("ready");
    });
  });

  it("signs in, stores the token, and displays the dashboard", async () => {
    fetchMock
      .mockResolvedValueOnce({
        json: async () => ({ status: "ok" }),
        ok: true,
      } as Response)
      .mockResolvedValueOnce({
        json: async () => ({
          access_token: "test-access-token",
          token_type: "bearer",
          expires_in: 1800,
        }),
        ok: true,
      } as Response)
      .mockResolvedValueOnce({
        json: async () => ({
          id: "user-1",
          email: "person@example.com",
          display_name: "Person",
          is_verified: false,
        }),
        ok: true,
      } as Response);

    render(<HomePage />);

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "person@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Hello, Person" })).toBeInTheDocument();
    });

    expect(window.sessionStorage.getItem("scraper-automation.access-token")).toBe(
      "test-access-token",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/auth/me",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
  });

  it("shows an API error when sign-in fails", async () => {
    fetchMock
      .mockResolvedValueOnce({
        json: async () => ({ status: "ok" }),
        ok: true,
      } as Response)
      .mockResolvedValueOnce({
        json: async () => ({ detail: "Incorrect email or password." }),
        ok: false,
        status: 401,
      } as Response);

    render(<HomePage />);

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "person@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("Incorrect email or password.");
  });
});
