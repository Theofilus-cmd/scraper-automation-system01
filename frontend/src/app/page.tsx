"use client";

import { useEffect, useState } from "react";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ReadyState = "checking" | "ready" | "unavailable";

export default function HomePage() {
  const [state, setState] = useState<ReadyState>("checking");

  useEffect(() => {
    let cancelled = false;

    async function checkReadiness() {
      try {
        const response = await fetch(`${API_BASE_URL}/readyz`);
        if (!cancelled) {
          setState(response.ok ? "ready" : "unavailable");
        }
      } catch {
        if (!cancelled) {
          setState("unavailable");
        }
      }
    }

    checkReadiness();
    const interval = setInterval(checkReadiness, 10_000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <main>
      <h1>Scraper Automation System</h1>
      <p>Phase 0 walking skeleton.</p>
      <p>
        API status: <span data-testid="api-status">{state}</span>
      </p>
    </main>
  );
}
