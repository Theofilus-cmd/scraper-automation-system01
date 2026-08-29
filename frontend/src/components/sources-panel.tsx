"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, createSource, listSources } from "../lib/api";
import type { Source } from "../lib/types";

type SourcesPanelProps = {
  token: string;
};

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "Something went wrong. Please try again.";
}

function formatStatus(status: Source["status"]): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export function SourcesPanel({ token }: SourcesPanelProps) {
  const [sources, setSources] = useState<Source[]>([]);
  const [url, setUrl] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);

  const loadSources = useCallback(async () => {
    setErrorMessage(null);
    setIsLoading(true);

    try {
      const response = await listSources(token);
      setSources(response.data);
    } catch (error) {
      setErrorMessage(messageFor(error));
    } finally {
      setIsLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

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
          {sources.map((source) => (
            <li key={source.id} className="source-card">
              <div>
                <a href={source.url} rel="noreferrer" target="_blank">
                  {source.url}
                </a>
                <p className="muted">Adapter: {source.adapter_type}</p>
              </div>
              <span className={`status status-${source.status}`}>
                {formatStatus(source.status)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
