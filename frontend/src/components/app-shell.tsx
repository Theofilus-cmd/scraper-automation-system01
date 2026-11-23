"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SessionProvider, useSession } from "../lib/session";
import { AuthPanel } from "./auth-panel";
import { LoadingState } from "./ui";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/alerts", label: "Alerts" },
  { href: "/dead-letter", label: "Dead letter" },
];

function Shell({ children }: { children: ReactNode }) {
  const { apiStatus, session, isRestoring, signIn, signOut } = useSession();
  const pathname = usePathname();

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

      {isRestoring ? (
        <LoadingState label="Restoring your session…" />
      ) : session ? (
        <>
          <nav aria-label="Main" className="main-nav">
            <ul>
              {NAV.map((item) => {
                const active =
                  item.href === "/" ? pathname === "/" : (pathname ?? "").startsWith(item.href);
                return (
                  <li key={item.href}>
                    <Link aria-current={active ? "page" : undefined} href={item.href}>
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
            <span className="muted nav-user">{session.user.email}</span>
            <button onClick={signOut} type="button">
              Sign out
            </button>
          </nav>
          {children}
        </>
      ) : (
        <AuthPanel onAuthenticated={signIn} />
      )}
    </main>
  );
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
