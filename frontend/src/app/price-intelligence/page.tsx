"use client";

import Link from "next/link";
import { useCallback } from "react";
import { PageHeader, LoadingState, ErrorState, EmptyState, StatusBadge } from "../../components/ui";
import { useAsync } from "../../hooks/use-async";
import { useSession } from "../../lib/session";
import { listPriceIntelligence } from "../../lib/api";
import type { PriceIntelligenceSummary } from "../../lib/types";

export default function PriceIntelligencePage() {
  const { session } = useSession();
  if (!session) return null;

  return <PriceIntelligence token={session.token} />;
}

function PriceIntelligence({ token }: { token: string }) {
  const load = useCallback(() => listPriceIntelligence({ token }), [token]);
  const { data, isLoading, error, reload } = useAsync(load);

  return (
    <div className="page">
      <PageHeader title="Price Intelligence" eyebrow="Market Overview" />

      {isLoading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : data?.data.length === 0 ? (
        <EmptyState 
          title="No intelligence data yet" 
          hint="Make sure you have products in your catalog and they are matched with competitor sources." 
        />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Internal Product</th>
                <th>Competitors</th>
                <th>Position</th>
                <th>Market Avg</th>
                <th>MAP Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data?.data.map((item) => (
                <tr key={item.internal_product.id}>
                  <td>
                    <strong>{item.internal_product.name}</strong>
                    <div className="muted small">{item.internal_product.sku}</div>
                  </td>
                  <td>{item.compared_count} / {item.competitor_count}</td>
                  <td>
                    {item.price_position ? (
                      <span className={`badge badge-${item.price_position}`}>
                        {item.price_position.toUpperCase()}
                      </span>
                    ) : "—"}
                  </td>
                  <td>
                    {item.market.avg_price ? `${item.market.avg_price} ${item.internal_product.currency}` : "—"}
                  </td>
                  <td>
                    {item.map.has_violation ? (
                      <span className="badge badge-failed">VIOLATION</span>
                    ) : (
                      <span className="badge badge-succeeded">OK</span>
                    )}
                  </td>
                  <td>
                    <Link href={`/price-intelligence/${item.internal_product.id}`}>
                      View Details
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
