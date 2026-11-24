"use client";

import { useParams } from "next/navigation";
import { useCallback } from "react";
import { PageHeader, LoadingState, ErrorState, PriceGap, StatusBadge } from "../../../components/ui";
import { useAsync } from "../../../hooks/use-async";
import { useSession } from "../../../lib/session";
import { getPriceIntelligenceDetails } from "../../../lib/api";

export default function PriceIntelligenceDetailPage() {
  const params = useParams();
  const id = params.id as string;
  const { session } = useSession();
  
  if (!session) return null;

  return <PriceIntelligenceDetails id={id} token={session.token} />;
}

function PriceIntelligenceDetails({ id, token }: { id: string; token: string }) {
  const load = useCallback(() => getPriceIntelligenceDetails({ token, internalProductId: id }), [id, token]);
  const { data, isLoading, error, reload } = useAsync(load);

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (!data) return null;

  const { internal_product: p, market, map, competitors, excluded } = data;

  return (
    <div className="page">
      <PageHeader title={p.name} eyebrow={`Intelligence Detail · ${p.sku}`}>
        <div className="badge badge-active">OWN PRICE: {p.own_price} {p.currency}</div>
      </PageHeader>

      <div className="tiles">
        <li>
          <span className="muted">Market Min</span>
          <span className="tile-value">{market.min_price || "—"}</span>
        </li>
        <li>
          <span className="muted">Market Avg</span>
          <span className="tile-value">{market.avg_price || "—"}</span>
        </li>
        <li>
          <span className="muted">Market Max</span>
          <span className="tile-value">{market.max_price || "—"}</span>
        </li>
        <li>
          <span className="muted">MAP Price</span>
          <span className="tile-value">{map.map_price || "—"}</span>
        </li>
      </div>

      <h3>Compared Competitors</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Competitor Product</th>
              <th>Price</th>
              <th>Gap</th>
              <th>MAP</th>
              <th>Status</th>
              <th>Last Scraped</th>
            </tr>
          </thead>
          <tbody>
            {competitors.map((c) => (
              <tr key={c.match_id}>
                <td>
                  <a href={c.product_url} target="_blank" rel="noopener noreferrer">
                    {c.product_name || "Unknown Product"}
                  </a>
                  <div className="muted small">{c.adapter_type}</div>
                </td>
                <td><strong>{c.price} {c.currency}</strong></td>
                <td><PriceGap gap={c.price_gap} pct={c.price_gap_pct} /></td>
                <td>
                  {c.map_violation ? (
                    <span className="badge badge-failed">VIOLATION</span>
                  ) : "—"}
                </td>
                <td><StatusBadge status={c.stock_status || "unknown"} /></td>
                <td>{new Date(c.scraped_at!).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {excluded.length > 0 && (
        <>
          <h3>Excluded Matches</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Product</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {excluded.map((e) => (
                  <tr key={e.match_id}>
                    <td>
                      <a href={e.product_url} target="_blank" rel="noopener noreferrer">
                        {e.product_url}
                      </a>
                    </td>
                    <td><span className="badge badge-failed">{e.reason}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
