import { afterEach, describe, expect, it, vi } from "vitest";

import { archiveSource, deleteSourceSchedule, upsertSourceSchedule } from "../src/lib/api";

describe("archiveSource", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("succeeds when the API returns 204 No Content", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 204,
      }),
    );

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      archiveSource({
        token: "test-token",
        sourceId: "source-123",
      }),
    ).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/sources/source-123",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.any(Headers),
      }),
    );
  });
});


describe("source schedule API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the requested interval when saving a schedule", async () => {
    const schedule = {
      id: "schedule-123",
      source_id: "source-123",
      interval_minutes: 60,
      is_active: true,
      next_run_at: "2026-08-30T12:00:00Z",
      last_run_at: null,
      created_at: "2026-08-30T11:00:00Z",
      updated_at: "2026-08-30T11:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(schedule), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      upsertSourceSchedule({
        token: "test-token",
        sourceId: "source-123",
        intervalMinutes: 60,
      }),
    ).resolves.toEqual(schedule);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/sources/source-123/schedule",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          interval_minutes: 60,
          is_active: true,
        }),
        headers: expect.any(Headers),
      }),
    );
  });

  it("succeeds when deleting a schedule returns 204 No Content", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 204,
      }),
    );

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      deleteSourceSchedule({
        token: "test-token",
        sourceId: "source-123",
      }),
    ).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/sources/source-123/schedule",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.any(Headers),
      }),
    );
  });
});
