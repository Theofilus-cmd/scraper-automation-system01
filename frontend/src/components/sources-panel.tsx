"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  ApiError,
  createSource,
  getRun,
  listRuns,
  listSources,
  triggerSourceRun,
} from "../lib/api";
import type { Run, RunStatus, Source } from "../lib/types";

type SourcesPanelProps = {
  token: string;
};

type SourceRunState = {
  error: string | null;
  isTriggering: boolean;
  run: Run | null;
  runId: string | null;
  status: RunStatus | null;
};

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "Something went wrong. Please try again.";
}

function formatStatus(status: string): string {
  return status
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function isRunInProgress(status: RunStatus | null): boolean {
  return status === "queued" || status === "pending" || status === "running";
}

export function SourcesPanel({ token }: SourcesPanelProps) {
  const [sources, setSources] = useState<Source[]>([]);
  const [url, setUrl] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [runsBySourceId, setRunsBySourceId] = useState<Record<string, SourceRunState>>(
    {},
  );
  const [runHistoryBySourceId, setRunHistoryBySourceId] = useState<Record<string, Run[]>>(
    {},
  );

  const loadRunHistory = useCallback(
    async (sourceIds: string[]) => {
      const results = await Promise.allSettled(
        sourceIds.map(async (sourceId) => {
          const response = await listRuns({ token, sourceId, limit: 3 });
          return { sourceId, runs: response.data };
        }),
      );

      setRunHistoryBySourceId((current) => {
        const next = { ...current };

        results.forEach((result) => {
          if (result.status === "fulfilled") {
            next[result.value.sourceId] = result.value.runs;
          }
        });

        return next;
      });
    },
    [token],
  );

  const loadSources = useCallback(async () => {
    setErrorMessage(null);
    setIsLoading(true);

    try {
      const response = await listSources(token);
      setSources(response.data);
      void loadRunHistory(response.data.map((source) => source.id));
    } catch (error) {
      setErrorMessage(messageFor(error));
    } finally {
      setIsLoading(false);
    }
  }, [loadRunHistory, token]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  useEffect(() => {
    const activeRuns = Object.entries(runsBySourceId).filter(
      ([, runState]) => runState.runId && isRunInProgress(runState.status),
    );

    if (activeRuns.length === 0) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      activeRuns.forEach(([sourceId, runState]) => {
        if (!runState.runId) {
          return;
        }

        void getRun({ token, runId: runState.runId })
          .then((run) => {
            setRunsBySourceId((current) => ({
              ...current,
              [sourceId]: {
                error: null,
                isTriggering: false,
                run,
                runId: run.id,
                status: run.status,
              },
            }));
          })
          .catch((error: unknown) => {
            setRunsBySourceId((current) => ({
              ...current,
              [sourceId]: {
                error: messageFor(error),
                isTriggering: false,
                run: current[sourceId]?.run ?? null,
                runId: current[sourceId]?.runId ?? null,
                status: null,
              },
            }));
          });
      });
    }, 2_000);

    return () => window.clearTimeout(timeoutId);
  }, [runsBySourceId, token]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setIsCreating(true);

    try {
      const source = await createSource({ token, url: url.trim() });
      setSources((currentSources) => {
        const withoutExisting = currentSources.filter(
          (currentSource) => currentSource.id !== source.id,
        );

        return [source, ...withoutExisting];
      });
      setUrl("");
    } catch (error) {
      setErrorMessage(messageFor(error));
    } finally {
      setIsCreating(false);
    }
  }

  async function handleRunNow(source: Source) {
    setRunsBySourceId((current) => ({
      ...current,
      [source.id]: {
        error: null,
        isTriggering: true,
        run: current[source.id]?.run ?? null,
        runId: current[source.id]?.runId ?? null,
        status: current[source.id]?.status ?? null,
      },
    }));

    try {
      const triggeredRun = await triggerSourceRun({
        token,
        sourceId: source.id,
      });

      setRunsBySourceId((current) => ({
        ...current,
        [source.id]: {
          error: null,
          isTriggering: false,
          run: null,
          runId: triggeredRun.run_id,
          status: triggeredRun.status,
        },
      }));

      const run = await getRun({
        token,
        runId: triggeredRun.run_id,
      });

      setRunsBySourceId((current) => ({
        ...current,
        [source.id]: {
          error: null,
          isTriggering: false,
          run,
          runId: run.id,
          status: run.status,
        },
      }));

      setRunHistoryBySourceId((current) => {
        const previous = current[source.id] ?? [];
        const withoutCurrentRun = previous.filter((item) => item.id !== run.id);

        return {
          ...current,
          [source.id]: [run, ...withoutCurrentRun].slice(0, 3),
        };
      });
    } catch (error) {
      setRunsBySourceId((current) => ({
        ...current,
        [source.id]: {
          error: messageFor(error),
          isTriggering: false,
          run: current[source.id]?.run ?? null,
          runId: current[source.id]?.runId ?? null,
          status: current[source.id]?.status ?? null,
        },
      }));
    }
  }

  return (
    <section
      aria-busy={isLoading}
      aria-labelledby="sources-heading"
      className="sources-panel"
    >
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Workspace sources</p>
          <h2 id="sources-heading">Monitor a product URL</h2>
          <p className="muted">
            Add a supported product page, then schedule a scrape or run it manually.
          </p>
        </div>
        <button disabled={isLoading} onClick={() => void loadSources()} type="button">
          {isLoading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

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

      {errorMessage ? (
        <p aria-live="polite" className="form-error" role="alert">
          {errorMessage}
        </p>
      ) : null}

      {isLoading ? (
        <p aria-live="polite" className="muted">
          Loading sources…
        </p>
      ) : sources.length === 0 ? (
        <div className="empty-dashboard">
          <h3>No sources yet</h3>
          <p>Add a product URL above to start monitoring it.</p>
        </div>
      ) : (
        <ul className="source-list">
          {sources.map((source) => {
            const runState = runsBySourceId[source.id];
            const runHistory = runHistoryBySourceId[source.id] ?? [];
            const isRunDisabled =
              runState?.isTriggering || isRunInProgress(runState?.status ?? null);

            return (
              <li key={source.id} className="source-card">
                <div className="source-card-details">
                  <a href={source.url} rel="noreferrer" target="_blank">
                    {source.url}
                  </a>
                  <p className="muted">Adapter: {source.adapter_type}</p>

                  {runState?.status ? (
                    <p aria-live="polite" className="run-status">
                      Latest run: {formatStatus(runState.status)}
                      {runState.run
                        ? ` · ${runState.run.succeeded_tasks}/${runState.run.total_tasks} tasks succeeded`
                        : null}
                    </p>
                  ) : null}

                  {runState?.error ? (
                    <p aria-live="polite" className="run-error" role="alert">
                      {runState.error}
                    </p>
                  ) : null}

                  {runHistory.length > 0 ? (
                    <div className="run-history">
                      <h3>Recent runs</h3>
                      <ul>
                        {runHistory.map((run) => (
                          <li key={run.id}>
                            {formatStatus(run.status)} ·{" "}
                            {run.triggered_by === "manual" ? "Manual" : "Scheduled"} ·{" "}
                            {run.succeeded_tasks}/{run.total_tasks} succeeded
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </div>

                <div className="source-card-actions">
                  <span className={`status status-${source.status}`}>
                    {formatStatus(source.status)}
                  </span>
                  <button
                    disabled={isRunDisabled}
                    onClick={() => void handleRunNow(source)}
                    type="button"
                  >
                    {runState?.isTriggering ? "Queueing…" : "Run now"}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
