"use client";

import Link from "next/link";
import { FormEvent, useCallback, useState } from "react";

import { DeleteSourceControl } from "../components/delete-source-control";
import { EmptyState, ErrorState, formatDateTime, LoadingState, PageHeader, StatusBadge } from "../components/ui";
import { errorMessage, useAsync } from "../hooks/use-async";
import { createSource, listAlerts, listInternalProducts, listPriceIntelligence, listSources, listTasks } from "../lib/api";
import { useSession } from "../lib/session";
import type { Source } from "../lib/types";

function Summary({ token, sources }: { token: string; sources: Source[] }) {
  const load = useCallback(async () => {
    const [alerts, dead, internal, intel] = await Promise.all([
      listAlerts({ token, limit: 100 }),
      listTasks({ token, status: "dead_letter", limit: 100 }),
      listInternalProducts({ token, limit: 1 }),
      listPriceIntelligence({ token, mapViolation: true, limit: 1 }),
    ]);
    return {
      alerts: alerts.data.length,
      more: alerts.pagination.has_more,
      dead: dead.data.length,
      deadMore: dead.pagination.has_more,
      catalogCount: internal.pagination.has_more ? "100+" : internal.data.length,
      mapViolations: intel.pagination.has_more ? "10+" : intel.data.length,
    };
  }, [token]);
  const { data } = useAsync(load);

  const count = (n: number | string | undefined, more?: boolean) =>
    n === undefined ? "…" : `${n}${more ? "+" : ""}`;

  return (
    <ul aria-label="Summary" className="tiles">
      <li>
        <span className="tile-value">{sources.filter((s) => s.status === "active").length}</span>
        <span className="muted">Active sources</span>
      </li>
      <li>
        <Link href="/internal-products">
          <span className="tile-value">{count(data?.catalogCount)}</span>
          <span className="muted">My Products</span>
        </Link>
      </li>
      <li>
        <Link href="/price-intelligence">
          <span className="tile-value" style={{ color: (data?.mapViolations && data.mapViolations !== 0) ? "#b91c1c" : "inherit" }}>
            {count(data?.mapViolations)}
          </span>
          <span className="muted">MAP Violations</span>
        </Link>
      </li>
      <li>
        <Link href="/alerts">
          <span className="tile-value">{count(data?.alerts, data?.more)}</span>
          <span className="muted">Alerts</span>
        </Link>
      </li>
    </ul>
  );
}

export default function DashboardPage() {
  const { session } = useSession();
  if (session === null) return null;
  return <Dashboard displayName={session.user.display_name} token={session.token} />;
}

function Dashboard({ displayName, token }: { displayName: string; token: string }) {
  const [extra, setExtra] = useState<Source[]>([]);
  const [url, setUrl] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [moreCursor, setMoreCursor] = useState<string | null | undefined>(undefined);
  const [removed, setRemoved] = useState<Set<string>>(new Set());

  const load = useCallback(() => listSources(token), [token]);
  const { data, error, isLoading, reload } = useAsync(load);

  const first = data?.data ?? [];
  const sources = [...first, ...extra].filter((s) => !removed.has(s.id));
  const nextCursor = moreCursor === undefined ? (data?.pagination.next_cursor ?? null) : moreCursor;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setIsCreating(true);
    try {
      await createSource({ token, url: url.trim() });
      setUrl("");
      setExtra([]);
      setMoreCursor(undefined);
      setRemoved(new Set());
      reload();
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setIsCreating(false);
    }
  }

  async function loadMore() {
    if (!nextCursor) return;
    setIsLoadingMore(true);
    try {
      const page = await listSources(token, { cursor: nextCursor });
      setExtra((current) => [...current, ...page.data]);
      setMoreCursor(page.pagination.next_cursor);
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      setIsLoadingMore(false);
    }
  }

  return (
    <section aria-labelledby="dashboard-heading" className="page">
      <PageHeader eyebrow="Signed in" title={`Hello, ${displayName}`}>
        <button disabled={isLoading} onClick={reload} type="button">
          {isLoading ? "Refreshing…" : "Refresh"}
        </button>
      </PageHeader>
      <h2 className="sr-only" id="dashboard-heading">
        Dashboard
      </h2>

      <Summary sources={sources} token={token} />

      <form className="source-form" onSubmit={handleSubmit}>
        <label>
          Product URL
          <input
            onChange={(event) => setUrl(event.target.value)}
            placeholder="http://mock-store:4000/products/example"
            required
            type="url"
            value={url}
          />
        </label>
        <button disabled={isCreating} type="submit">
          {isCreating ? "Adding…" : "Add source"}
        </button>
      </form>
      {formError ? (
        <p className="form-error" role="alert">
          {formError}
        </p>
      ) : null}

      <h3>Sources</h3>
      {isLoading && data === null ? (
        <LoadingState label="Loading sources…" />
      ) : error && data === null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : sources.length === 0 ? (
        <EmptyState hint="Add a supported product URL above to start monitoring." title="No sources yet" />
      ) : (
        <ul className="card-list">
          {sources.map((source) => (
            <li className="card" key={source.id}>
              <div className="card-main">
                <Link className="card-title" href={`/sources/${source.id}`}>
                  {source.url}
                </Link>
                <p className="muted">
                  {source.adapter_type} · added {formatDateTime(source.created_at)}
                </p>
              </div>
              <StatusBadge status={source.status} />
              {source.status !== "archived" ? (
                <DeleteSourceControl
                  onDeleted={() => setRemoved((current) => new Set(current).add(source.id))}
                  sourceId={source.id}
                  sourceUrl={source.url}
                  token={token}
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {nextCursor ? (
        <button disabled={isLoadingMore} onClick={() => void loadMore()} type="button">
          {isLoadingMore ? "Loading…" : "Load more"}
        </button>
      ) : null}
    </section>
  );
}
