"use client";

import { useCallback, useState } from "react";
import { PageHeader, LoadingState, ErrorState, EmptyState, StatusBadge } from "../../components/ui";
import { useAsync } from "../../hooks/use-async";
import { useSession } from "../../lib/session";
import { listInternalProducts, createInternalProduct } from "../../lib/api";
import type { InternalProduct } from "../../lib/types";

export default function InternalProductsPage() {
  const { session } = useSession();
  if (!session) return null;

  return <InternalProducts token={session.token} />;
}

function InternalProducts({ token }: { token: string }) {
  const [isAdding, setIsAdding] = useState(false);
  const [name, setName] = useState("");
  const [sku, setSku] = useState("");
  const [price, setPrice] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => listInternalProducts({ token }), [token]);
  const { data, isLoading, error: loadError, reload } = useAsync(load);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await createInternalProduct({
        token,
        name,
        sku,
        own_price: price,
        currency,
      });
      setName("");
      setSku("");
      setPrice("");
      setIsAdding(false);
      reload();
    } catch (err: any) {
      setError(err.message);
    }
  }

  return (
    <div className="page">
      <PageHeader title="My Catalog">
        <button onClick={() => setIsAdding(!isAdding)} type="button">
          {isAdding ? "Cancel" : "Add Product"}
        </button>
      </PageHeader>

      {isAdding && (
        <form className="card-block" onSubmit={handleAdd}>
          <h3>Add Internal Product</h3>
          <div className="grid-2">
            <label>
              Product Name
              <input required value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label>
              SKU
              <input required value={sku} onChange={(e) => setSku(e.target.value)} />
            </label>
            <label>
              Own Price
              <input required type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} />
            </label>
            <label>
              Currency
              <input required value={currency} onChange={(e) => setCurrency(e.target.value)} />
            </label>
          </div>
          {error && <p className="form-error">{error}</p>}
          <button type="submit">Create Product</button>
        </form>
      )}

      {isLoading ? (
        <LoadingState />
      ) : loadError ? (
        <ErrorState message={loadError} onRetry={reload} />
      ) : data?.data.length === 0 ? (
        <EmptyState title="No products in catalog" hint="Add your own products to start matching with competitors." />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>SKU</th>
                <th>Price</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data?.data.map((p) => (
                <tr key={p.id}>
                  <td><strong>{p.name}</strong></td>
                  <td>{p.sku}</td>
                  <td>{p.own_price} {p.currency}</td>
                  <td><StatusBadge status={p.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
