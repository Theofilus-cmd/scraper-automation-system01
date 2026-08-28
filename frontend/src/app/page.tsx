"use client";

import { useEffect, useState } from "react";

import { AuthPanel } from "../components/auth-panel";
import { ApiError, checkApiReadiness, getCurrentUser } from "../lib/api";
import {
  clearStoredAccessToken,
  getStoredAccessToken,
  storeAccessToken,
} from "../lib/auth-storage";
import type { ApiStatus, AuthSession, User } from "../lib/types";

export default function HomePage() {
  const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");
  const [session, setSession] = useState<AuthSession | null>(null);
  const [isRestoringSession, setIsRestoringSession] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function checkReadiness() {
      try {
        await checkApiReadiness();
        if (!cancelled) {
          setApiStatus("ready");
        }
      } catch {
        if (!cancelled) {
          setApiStatus("unavailable");
        }
      }
    }

    void checkReadiness();
    const interval = window.setInterval(() => {
      void checkReadiness();
    }, 10_000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const token = getStoredAccessToken();

    async function restoreSession() {
      if (token === null) {
        if (!cancelled) {
          setIsRestoringSession(false);
        }
        return;
      }

      try {
        const user = await getCurrentUser(token);
        if (!cancelled) {
          setSession({ token, user });
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          clearStoredAccessToken();
        }
      } finally {
        if (!cancelled) {
          setIsRestoringSession(false);
        }
      }
    }

    void restoreSession();

    return () => {
      cancelled = true;
    };
  }, []);

  function handleAuthenticated(token: string, user: User) {
    storeAccessToken(token);
    setSession({ token, user });
  }

  function handleLogout() {
    clearStoredAccessToken();
    setSession(null);
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <p className="eyebrow">Product monitoring workspace</p>
          <h1>Scraper Automation System</h1>
        </div>
        <p className="api-status">
          API status: <span data-testid="api-status">{apiStatus}</span>
        </p>
      </header>

      {isRestoringSession ? (
        <section aria-live="polite" className="loading-panel">
          Restoring your session…
        </section>
      ) : session ? (
        <section aria-labelledby="dashboard-heading" className="dashboard-panel">
          <div className="dashboard-heading">
            <div>
              <p className="eyebrow">Signed in</p>
              <h2 id="dashboard-heading">Hello, {session.user.display_name}</h2>
              <p className="muted">{session.user.email}</p>
            </div>
            <button onClick={handleLogout} type="button">
              Sign out
            </button>
          </div>

          <div className="empty-dashboard">
            <h3>Your monitoring dashboard is ready</h3>
            <p>
              The next step adds source creation, scheduled scraping, run status,
              products, and change history here.
            </p>
          </div>
        </section>
      ) : (
        <AuthPanel onAuthenticated={handleAuthenticated} />
      )}
    </main>
  );
}
