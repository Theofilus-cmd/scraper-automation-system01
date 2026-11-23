"use client";

import { type ReactNode, useCallback, useState } from "react";

import { errorMessage, useAsync } from "../hooks/use-async";
import type { PaginatedResponse } from "../lib/types";
import { EmptyState, ErrorState, LoadingState } from "./ui";

/** Cursor-paginated list with loading / empty / error states and "Load more". */
export function PagedList<T extends { id: string }>({
  fetchPage,
  empty,
  renderTable,
  loadingLabel,
}: {
  fetchPage: (cursor: string | null) => Promise<PaginatedResponse<T>>;
  empty: { title: string; hint?: string };
  renderTable: (items: T[]) => ReactNode;
  loadingLabel: string;
}) {
  const load = useCallback(() => fetchPage(null), [fetchPage]);
  const first = useAsync(load);
  const [more, setMore] = useState<{ items: T[]; cursor: string | null } | null>(null);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  if (first.isLoading && first.data === null) return <LoadingState label={loadingLabel} />;
  if (first.error && first.data === null) return <ErrorState message={first.error} onRetry={first.reload} />;

  const items = [...(first.data?.data ?? []), ...(more?.items ?? [])];
  const cursor = more ? more.cursor : (first.data?.pagination.next_cursor ?? null);
  if (items.length === 0) return <EmptyState {...empty} />;

  async function loadMore() {
    setIsLoadingMore(true);
    setMoreError(null);
    try {
      const page = await fetchPage(cursor);
      setMore({ items: [...(more?.items ?? []), ...page.data], cursor: page.pagination.next_cursor });
    } catch (err) {
      setMoreError(errorMessage(err));
    } finally {
      setIsLoadingMore(false);
    }
  }

  return (
    <>
      {renderTable(items)}
      {moreError ? (
        <p className="form-error" role="alert">
          {moreError}
        </p>
      ) : null}
      {cursor ? (
        <button disabled={isLoadingMore} onClick={() => void loadMore()} type="button">
          {isLoadingMore ? "Loading…" : "Load more"}
        </button>
      ) : null}
    </>
  );
}
