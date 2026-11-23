"use client";

import Link from "next/link";
import { useCallback } from "react";

import { PagedList } from "../../components/paged-list";
import { formatDateTime, PageHeader } from "../../components/ui";
import { listTasks } from "../../lib/api";
import { useSession } from "../../lib/session";

export default function DeadLetterPage() {
  const { session } = useSession();
  const token = session?.token ?? "";
  const fetchPage = useCallback(
    (cursor: string | null) => listTasks({ token, status: "dead_letter", cursor }),
    [token],
  );
  if (session === null) return null;

  return (
    <section className="page">
      <PageHeader eyebrow="Reliability" title="Dead letter" />
      <p className="muted">Tasks that exhausted all retry attempts and need attention.</p>
      <PagedList
        empty={{ title: "No dead-lettered tasks", hint: "Everything is running smoothly." }}
        fetchPage={fetchPage}
        loadingLabel="Loading dead-lettered tasks…"
        renderTable={(tasks) => (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Finished</th>
                  <th>Reason</th>
                  <th>Attempts</th>
                  <th>Links</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id}>
                    <td>{formatDateTime(t.finished_at ?? t.queued_at)}</td>
                    <td>{t.error_reason ?? "unknown"}</td>
                    <td>
                      {t.attempt_count}/{t.max_attempts}
                    </td>
                    <td>
                      <Link href={`/runs/${t.run_id}`}>Run</Link> ·{" "}
                      <Link href={`/sources/${t.source_id}`}>Source</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      />
    </section>
  );
}
