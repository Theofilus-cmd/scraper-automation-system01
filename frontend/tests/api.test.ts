import { afterEach, describe, expect, it, vi } from "vitest";

import { archiveSource } from "../src/lib/api";

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
