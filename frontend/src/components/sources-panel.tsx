"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  ApiError,
  archiveSource,
  updateSourceStatus,
  createSource,
  getRun,
  getSource,
  listRuns,
  listSources,
  triggerSourceRun,
  unarchiveSource,
  upsertSourceSchedule,
  deleteSourceSchedule,
} from "../lib/api";
import type { Run, RunStatus, Schedule, Source } from "../lib/types";

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

function formatDateTime(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
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
  const [isUpdatingSourceId, setIsUpdatingSourceId] = useState<string | null>(null);
  const [isSavingScheduleSourceId, setIsSavingScheduleSourceId] = useState<string | null>(null);
  const [scheduleIntervalBySourceId, setScheduleIntervalBySourceId] = useState<Record<string, number>>({});
  const [schedulesBySourceId, setSchedulesBySourceId] = useState<Record<string, Schedule | null>>({});
  const [sourceActionErrors, setSourceActionErrors] = useState<Record<string, string>>({});
  const [sourceActionSuccesses, setSourceActionSuccesses] = useState<Record<string, string>>({});
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

  const loadSchedules = useCallback(
    async (sourceIds: string[]) => {
      const results = await Promise.allSettled(
        sourceIds.map(async (sourceId) => {
          const source = await getSource({ token, sourceId });
          return { sourceId, schedule: source.schedule };
        }),
      );

      setSchedulesBySourceId((current) => {
        const next = { ...current };

        results.forEach((result) => {
          if (result.status === "fulfilled") {
            next[result.value.sourceId] = result.value.schedule;
          }
        });

        return next;
      });

      setScheduleIntervalBySourceId((current) => {
        const next = { ...current };

        results.forEach((result) => {
          if (result.status === "fulfilled" && result.value.schedule !== null) {
            next[result.value.sourceId] = result.value.schedule.interval_minutes;
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
      const sourceIds = response.data.map((source) => source.id);
      void loadRunHistory(sourceIds);
      void loadSchedules(sourceIds);
    } catch (error) {
      setErrorMessage(messageFor(error));
    } finally {
      setIsLoading(false);
    }
  }, [loadRunHistory, loadSchedules, token]);

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

  function clearSourceActionMessages(sourceId: string) {
    setSourceActionErrors((current) => {
      const next = { ...current };
      delete next[sourceId];
      return next;
    });
    setSourceActionSuccesses((current) => {
      const next = { ...current };
      delete next[sourceId];
      return next;
    });
  }

  function setSourceActionSuccess(sourceId: string, message: string) {
    setSourceActionSuccesses((current) => ({
      ...current,
      [sourceId]: message,
    }));
  }

  async function handleArchiveSource(source: Source) {
    if (!window.confirm(`Archive ${source.url}? Scheduled runs will be disabled.`)) {
      return;
    }

    setIsUpdatingSourceId(source.id);
    clearSourceActionMessages(source.id);

    try {
      await archiveSource({ token, sourceId: source.id });
      setSources((currentSources) =>
        currentSources.map((currentSource) =>
          currentSource.id === source.id
            ? { ...currentSource, status: "archived" }
            : currentSource,
        ),
      );
      setSourceActionSuccess(source.id, "Source archived.");
    } catch (error) {
      setSourceActionErrors((current) => ({
        ...current,
        [source.id]: messageFor(error),
      }));
    } finally {
      setIsUpdatingSourceId(null);
    }
  }

  async function handleUnarchiveSource(source: Source) {
    setIsUpdatingSourceId(source.id);
    clearSourceActionMessages(source.id);

    try {
      const updatedSource = await unarchiveSource({ token, sourceId: source.id });
      setSources((currentSources) =>
        currentSources.map((currentSource) =>
          currentSource.id === updatedSource.id ? updatedSource : currentSource,
        ),
      );
      setSourceActionSuccess(source.id, "Source unarchived.");
    } catch (error) {
      setSourceActionErrors((current) => ({
        ...current,
        [source.id]: messageFor(error),
      }));
    } finally {
      setIsUpdatingSourceId(null);
    }
  }

  async function handleSourceStatusChange(
    source: Source,
    status: "active" | "paused",
  ) {
    setIsUpdatingSourceId(source.id);
    clearSourceActionMessages(source.id);

    try {
      const updatedSource = await updateSourceStatus({
        token,
        sourceId: source.id,
        status,
      });

      setSources((currentSources) =>
        currentSources.map((currentSource) =>
          currentSource.id === updatedSource.id ? updatedSource : currentSource,
        ),
      );
      setSourceActionSuccess(
        source.id,
        status === "paused" ? "Source paused." : "Source resumed.",
      );
    } catch (error) {
      setSourceActionErrors((current) => ({
        ...current,
        [source.id]: messageFor(error),
      }));
    } finally {
      setIsUpdatingSourceId(null);
    }
  }

  async function handleSaveSchedule(source: Source) {
    const intervalMinutes = scheduleIntervalBySourceId[source.id] ?? 15;

    setIsSavingScheduleSourceId(source.id);
    clearSourceActionMessages(source.id);

    try {
      const schedule = await upsertSourceSchedule({
        token,
        sourceId: source.id,
        intervalMinutes,
      });
      setSchedulesBySourceId((current) => ({
        ...current,
        [source.id]: schedule,
      }));
      setSourceActionSuccess(source.id, "Schedule saved.");
    } catch (error) {
      setSourceActionErrors((current) => ({
        ...current,
        [source.id]: messageFor(error),
      }));
    } finally {
      setIsSavingScheduleSourceId(null);
    }
  }

  async function handleDeleteSchedule(source: Source) {
    setIsSavingScheduleSourceId(source.id);
    clearSourceActionMessages(source.id);

    try {
      await deleteSourceSchedule({
        token,
        sourceId: source.id,
      });
      setSchedulesBySourceId((current) => ({
        ...current,
        [source.id]: null,
      }));
      setSourceActionSuccess(source.id, "Schedule removed.");
    } catch (error) {
      setSourceActionErrors((current) => ({
        ...current,
        [source.id]: messageFor(error),
      }));
    } finally {
      setIsSavingScheduleSourceId(null);
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
            const schedule = schedulesBySourceId[source.id] ?? null;
            const isUpdatingSource = isUpdatingSourceId === source.id;
            const isRunDisabled =
              isUpdatingSource ||
              runState?.isTriggering ||
              isRunInProgress(runState?.status ?? null);

            return (
              <li key={source.id} className="source-card">
                <div className="source-card-details">
                  <a href={source.url} rel="noreferrer" target="_blank">
                    {source.url}
                  </a>
                  <p className="muted">Adapter: {source.adapter_type}</p>

                  {schedule ? (
                    <>
                      <p className="muted">
                        Runs every {schedule.interval_minutes} minutes
                      </p>
                      {schedule.next_run_at ? (
                        <p className="muted">
                          Next run: {formatDateTime(schedule.next_run_at)}
                        </p>
                      ) : null}
                    </>
                  ) : (
                    <p className="muted">No schedule configured</p>
                  )}

                  {sourceActionSuccesses[source.id] ? (
                    <p aria-live="polite" className="run-success" role="status">
                      {sourceActionSuccesses[source.id]}
                    </p>
                  ) : null}

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

                  {sourceActionErrors[source.id] ? (
                    <p aria-live="polite" className="run-error" role="alert">
                      {sourceActionErrors[source.id]}
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

                  {source.status !== "archived" ? (
                    <button
                      className="source-action-button source-action-secondary"
                      disabled={isUpdatingSource}
                      onClick={() =>
                        void handleSourceStatusChange(
                          source,
                          source.status === "active" ? "paused" : "active",
                        )
                      }
                      type="button"
                    >
                      {isUpdatingSource
                        ? "Updating…"
                        : source.status === "active"
                          ? "Pause"
                          : "Resume"}
                    </button>
                  ) : null}

                  {source.status === "archived" ? (
                    <button
                      className="source-action-button source-action-secondary"
                      disabled={isUpdatingSource}
                      onClick={() => void handleUnarchiveSource(source)}
                      type="button"
                    >
                      {isUpdatingSource ? "Updating…" : "Unarchive"}
                    </button>
                  ) : (
                    <button
                      className="source-action-button source-action-danger"
                      disabled={isUpdatingSource}
                      onClick={() => void handleArchiveSource(source)}
                      type="button"
                    >
                      {isUpdatingSource ? "Updating…" : "Archive"}
                    </button>
                  )}

                  <label className="schedule-control">
                    Schedule
                    <select
                      disabled={
                        source.status === "archived" ||
                        isUpdatingSource ||
                        isSavingScheduleSourceId === source.id
                      }
                      onChange={(event) =>
                        setScheduleIntervalBySourceId((current) => ({
                          ...current,
                          [source.id]: Number(event.target.value),
                        }))
                      }
                      value={scheduleIntervalBySourceId[source.id] ?? 15}
                    >
                      <option value={15}>Every 15 minutes</option>
                      <option value={30}>Every 30 minutes</option>
                      <option value={60}>Every hour</option>
                      <option value={360}>Every 6 hours</option>
                      <option value={1440}>Every day</option>
                    </select>
                  </label>

                  <button
                    className="source-action-button source-action-secondary"
                    disabled={
                      source.status === "archived" ||
                      isUpdatingSource ||
                      isSavingScheduleSourceId === source.id
                    }
                    onClick={() => void handleSaveSchedule(source)}
                    type="button"
                  >
                    {isSavingScheduleSourceId === source.id
                      ? "Saving schedule…"
                      : "Save schedule"}
                  </button>

                  {schedule ? (
                    <button
                      className="source-action-button source-action-danger"
                      disabled={
                        source.status === "archived" ||
                        isUpdatingSource ||
                        isSavingScheduleSourceId === source.id
                      }
                      onClick={() => void handleDeleteSchedule(source)}
                      type="button"
                    >
                      {isSavingScheduleSourceId === source.id
                        ? "Removing schedule…"
                        : "Remove schedule"}
                    </button>
                  ) : null}

                  <button
                    className="source-action-button source-action-primary"
                    disabled={isRunDisabled || source.status === "archived"}
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
