"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { PagedList } from "../../components/paged-list";
import { formatDateTime, formatLabel, PageHeader, StatusBadge } from "../../components/ui";
import { listAlerts } from "../../lib/api";
import { useSession } from "../../lib/session";

export default function AlertsPage() {
  const { session } = useSession();
  const [kind, setKind] = useState("");
  const token = session?.token ?? "";
  const fetchPage = useCallback(
    (cursor: string | null) => listAlerts({ token, kind: kind || undefined, cursor }),
    [token, kind],
  );
  if (session === null) return null;

  return (
    <section className="page">
      <PageHeader eyebrow="Monitoring" title="Alerts">
        <label className="inline-label">
          Type
          <select onChange={(e) => setKind(e.target.value)} value={kind}>
            <option value="">All</option>
            <option value="price_changed">Price changed</option>
            <option value="stock_status_changed">Stock changed</option>
          </select>
        </label>
      </PageHeader>
      <PagedList
        empty={{ title: "No alerts", hint: "Alerts appear when a price or stock status changes." }}
        fetchPage={fetchPage}
        key={kind}
        loadingLabel="Loading alerts…"
        renderTable={(alerts) => (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Product</th>
                  <th>Change</th>
                  <th>Email</th>
                </tr>
              </thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id}>
                    <td>{formatDateTime(a.created_at)}</td>
                    <td>
                      <Link href={`/sources/${a.source_id}`}>{a.product_name}</Link>
                    </td>
                    <td>
                      {formatLabel(a.kind)}: {a.old_value} → {a.new_value}
                      {a.kind === "price_changed" && a.currency ? ` ${a.currency}` : ""}
                    </td>
                    <td>
                      <StatusBadge status={a.email_status} />
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
