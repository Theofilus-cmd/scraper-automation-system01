"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { DeleteSourceControl } from "../../../components/delete-source-control";
import { LineChart, type ChartPoint } from "../../../components/line-chart";
import {
  EmptyState,
  ErrorState,
  formatDateTime,
  formatLabel,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../../../components/ui";
import { errorMessage, useAsync } from "../../../hooks/use-async";
import {
  deleteSourceSchedule,
  getSource,
  listAlerts,
  listProductHistory,
  listRuns,
  triggerSourceRun,
  unarchiveSource,
  updateSourceStatus,
  upsertSourceSchedule,
} from "../../../lib/api";
import { useSession } from "../../../lib/session";
import type { ObservationHistoryRow } from "../../../lib/types";

const INTERVALS = [15, 30, 60, 360, 1440];

function priceSeries(rows: ObservationHistoryRow[]): { points: ChartPoint[]; currency: string | null } {
  const points: ChartPoint[] = [];
  let currency: string | null = null;
  for (const row of rows) {
    const t = Date.parse(row.scraped_at ?? row.version_created_at ?? "");
    const v = row.price === null ? NaN : Number(row.price);
    if (Number.isNaN(t) || Number.isNaN(v)) continue;
    currency = row.currency ?? currency;
    points.push({ t, v });
  }
  return { points, currency };
}

function stockSeries(rows: ObservationHistoryRow[]): ChartPoint[] {
  const points: ChartPoint[] = [];
  for (const row of rows) {
    const t = Date.parse(row.scraped_at ?? row.version_created_at ?? "");
    if (Number.isNaN(t)) continue;
    points.push({ t, v: row.stock_status === "in_stock" ? 1 : row.stock_status === "out_of_stock" ? 0 : 0.5 });
  }
  return points;
}

export default function SourceDetailPage() {
  const { session } = useSession();
  const params = useParams<{ id: string }>();
  if (session === null || !params?.id) return null;
  return <SourceDetail sourceId={params.id} token={session.token} />;
}

function SourceDetail({ sourceId, token }: { sourceId: string; token: string }) {
  const router = useRouter();
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [interval, setIntervalMinutes] = useState(60);

  const loadSource = useCallback(() => getSource({ token, sourceId }), [token, sourceId]);
  const source = useAsync(loadSource);

  const productId = source.data?.current_product?.product.id ?? null;
  const loadHistory = useCallback(
    async () =>
      productId ? (await listProductHistory({ token, productId, limit: 100 })).data : [],
    [token, productId],
  );
  const history = useAsync(loadHistory);

  const loadRuns = useCallback(() => listRuns({ token, sourceId, limit: 10 }), [token, sourceId]);
  const runs = useAsync(loadRuns);

  const loadAlerts = useCallback(() => listAlerts({ token, sourceId, limit: 10 }), [token, sourceId]);
  const alerts = useAsync(loadAlerts);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    setActionNotice(null);
    try {
      await fn();
      setActionNotice(label);
      source.reload();
      runs.reload();
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (source.isLoading && source.data === null) return <LoadingState label="Loading source…" />;
  if (source.error || source.data === null) {
    return <ErrorState message={source.error ?? "Source not found."} onRetry={source.reload} />;
  }

  const s = source.data;
  const obs = s.current_product?.observation ?? null;
  const rows = history.data ?? [];
  // History is newest-first; charts sort by time themselves.
  const { points: prices, currency } = priceSeries(rows);
  const stock = stockSeries(rows);

  return (
    <section className="page">
      <p>
        <Link href="/">← Back to dashboard</Link>
      </p>
      <PageHeader eyebrow={s.adapter_type} title={s.url}>
        <StatusBadge status={s.status} />
      </PageHeader>

      {actionNotice ? (
        <p className="form-success" role="status">
          {actionNotice}
        </p>
      ) : null}
      {actionError ? (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      ) : null}

      <div className="toolbar">
        {s.status === "archived" ? (
          <button disabled={busy} onClick={() => void act("Source restored.", () => unarchiveSource({ token, sourceId }))} type="button">
            Unarchive
          </button>
        ) : (
          <>
            <button
              className="source-action-primary"
              disabled={busy}
              onClick={() =>
                void act("Run started.", async () => {
                  await triggerSourceRun({ token, sourceId });
                })
              }
              type="button"
            >
              Run now
            </button>
            <button
              disabled={busy}
              onClick={() =>
                void act(s.status === "paused" ? "Source resumed." : "Source paused.", () =>
                  updateSourceStatus({ token, sourceId, status: s.status === "paused" ? "active" : "paused" }),
                )
              }
              type="button"
            >
              {s.status === "paused" ? "Resume" : "Pause"}
            </button>
            <DeleteSourceControl
              onDeleted={() => router.push("/")}
              sourceId={sourceId}
              sourceUrl={s.url}
              token={token}
            />
          </>
        )}
      </div>

      <div className="grid-2">
        <section aria-labelledby="schedule-heading" className="card-block">
          <h3 id="schedule-heading">Schedule</h3>
          {s.schedule ? (
            <p>
              Every {s.schedule.interval_minutes} min · next run {formatDateTime(s.schedule.next_run_at)}
            </p>
          ) : (
            <p className="muted">No schedule configured.</p>
          )}
          {s.status !== "archived" ? (
            <div className="inline-form">
              <label>
                Interval
                <select onChange={(e) => setIntervalMinutes(Number(e.target.value))} value={interval}>
                  {INTERVALS.map((m) => (
                    <option key={m} value={m}>
                      {m >= 60 ? `${m / 60} h` : `${m} min`}
                    </option>
                  ))}
                </select>
              </label>
              <button
                disabled={busy}
                onClick={() => void act("Schedule saved.", () => upsertSourceSchedule({ token, sourceId, intervalMinutes: interval }))}
                type="button"
              >
                Save schedule
              </button>
              {s.schedule ? (
                <button
                  disabled={busy}
                  onClick={() => void act("Schedule removed.", () => deleteSourceSchedule({ token, sourceId }))}
                  type="button"
                >
                  Remove
                </button>
              ) : null}
            </div>
          ) : null}
        </section>

        <section aria-labelledby="current-heading" className="card-block">
          <h3 id="current-heading">Current product</h3>
          {obs ? (
            <dl className="facts">
              <dt>Name</dt>
              <dd>{obs.product_name ?? "—"}</dd>
              <dt>Price</dt>
              <dd>{obs.price ? `${obs.price} ${obs.currency ?? ""}` : "—"}</dd>
              <dt>Stock</dt>
              <dd>{formatLabel(obs.stock_status)}</dd>
              <dt>Scraped</dt>
              <dd>{formatDateTime(obs.scraped_at)}</dd>
            </dl>
          ) : (
            <p className="muted">No data scraped yet. Run the source to collect it.</p>
          )}
        </section>
      </div>

      <section aria-labelledby="history-heading" className="card-block">
        <h3 id="history-heading">Price &amp; stock history</h3>
        {history.isLoading && productId ? (
          <LoadingState label="Loading history…" />
        ) : history.error ? (
          <ErrorState message={history.error} onRetry={history.reload} />
        ) : rows.length === 0 ? (
          <EmptyState hint="History appears after the first successful scrape." title="No history yet" />
        ) : (
          <div className="grid-2">
            {prices.length > 0 ? (
              <LineChart
                formatValue={(v) => `${v}${currency ? ` ${currency}` : ""}`}
                points={prices}
                title="Price"
              />
            ) : (
              <EmptyState title="No price data" />
            )}
            <LineChart
              color="#047857"
              formatValue={(v) => (v === 1 ? "In stock" : v === 0 ? "Out of stock" : "Unknown")}
              points={stock}
              step
              title="Stock status"
              yTicks={[
                { value: 0, label: "Out" },
                { value: 0.5, label: "Unknown" },
                { value: 1, label: "In" },
              ]}
            />
          </div>
        )}
      </section>

      <div className="grid-2">
        <section aria-labelledby="runs-heading" className="card-block">
          <h3 id="runs-heading">Recent runs</h3>
          {runs.isLoading && runs.data === null ? (
            <LoadingState />
          ) : runs.error ? (
            <ErrorState message={runs.error} onRetry={runs.reload} />
          ) : (runs.data?.data.length ?? 0) === 0 ? (
            <EmptyState title="No runs yet" />
          ) : (
            <ul className="plain-list">
              {runs.data?.data.map((run) => (
                <li key={run.id}>
                  <Link href={`/runs/${run.id}`}>{formatDateTime(run.created_at)}</Link>{" "}
                  <StatusBadge status={run.status} /> <span className="muted">{run.triggered_by}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="source-alerts-heading" className="card-block">
          <h3 id="source-alerts-heading">Recent alerts</h3>
          {alerts.isLoading && alerts.data === null ? (
            <LoadingState />
          ) : alerts.error ? (
            <ErrorState message={alerts.error} onRetry={alerts.reload} />
          ) : (alerts.data?.data.length ?? 0) === 0 ? (
            <EmptyState title="No alerts for this source" />
          ) : (
            <ul className="plain-list">
              {alerts.data?.data.map((alert) => (
                <li key={alert.id}>
                  {formatLabel(alert.kind)}: {alert.old_value} → {alert.new_value}{" "}
                  <span className="muted">{formatDateTime(alert.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </section>
  );
}
