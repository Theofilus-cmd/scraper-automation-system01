"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback } from "react";

import { ErrorState, EmptyState, formatDateTime, LoadingState, PageHeader, StatusBadge } from "../../../components/ui";
import { useAsync } from "../../../hooks/use-async";
import { getRun, listRunTasks } from "../../../lib/api";
import { useSession } from "../../../lib/session";

export default function RunDetailPage() {
  const { session } = useSession();
  const params = useParams<{ id: string }>();
  if (session === null || !params?.id) return null;
  return <RunDetail runId={params.id} token={session.token} />;
}

function RunDetail({ runId, token }: { runId: string; token: string }) {
  const loadRun = useCallback(() => getRun({ token, runId }), [token, runId]);
  const run = useAsync(loadRun);
  const loadTasks = useCallback(() => listRunTasks({ token, runId }), [token, runId]);
  const tasks = useAsync(loadTasks);

  if (run.isLoading && run.data === null) return <LoadingState label="Loading run…" />;
  if (run.error || run.data === null) {
    return <ErrorState message={run.error ?? "Run not found."} onRetry={run.reload} />;
  }
  const r = run.data;

  return (
    <section className="page">
      <p>
        <Link href={`/sources/${r.source_id}`}>← Back to source</Link>
      </p>
      <PageHeader eyebrow="Run" title={`Run ${r.id.slice(0, 8)}`}>
        <StatusBadge status={r.status} />
        <button
          onClick={() => {
            run.reload();
            tasks.reload();
          }}
          type="button"
        >
          Refresh
        </button>
      </PageHeader>

      <dl className="facts card-block">
        <dt>Triggered by</dt>
        <dd>{r.triggered_by}</dd>
        <dt>Created</dt>
        <dd>{formatDateTime(r.created_at)}</dd>
        <dt>Started</dt>
        <dd>{formatDateTime(r.started_at)}</dd>
        <dt>Finished</dt>
        <dd>{formatDateTime(r.finished_at)}</dd>
        <dt>Tasks</dt>
        <dd>
          {r.succeeded_tasks} succeeded · {r.failed_tasks} failed · {r.total_tasks} total
        </dd>
      </dl>

      <h3>Tasks</h3>
      {tasks.isLoading && tasks.data === null ? (
        <LoadingState label="Loading tasks…" />
      ) : tasks.error ? (
        <ErrorState message={tasks.error} onRetry={tasks.reload} />
      ) : (tasks.data?.data.length ?? 0) === 0 ? (
        <EmptyState title="No tasks for this run" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Attempts</th>
                <th>Error</th>
                <th>Queued</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {tasks.data?.data.map((task) => (
                <tr key={task.id}>
                  <td>
                    <StatusBadge status={task.status} />
                  </td>
                  <td>
                    {task.attempt_count}/{task.max_attempts}
                  </td>
                  <td>{task.error_reason ?? "—"}</td>
                  <td>{formatDateTime(task.queued_at)}</td>
                  <td>{formatDateTime(task.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
