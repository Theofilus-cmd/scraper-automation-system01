"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SessionProvider, useSession } from "../lib/session";
import { AuthPanel } from "./auth-panel";
import { LoadingState } from "./ui";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/internal-products", label: "My Catalog" },
  { href: "/price-intelligence", label: "Price Intelligence" },
  { href: "/alerts", label: "Alerts" },
  { href: "/dead-letter", label: "Dead Letter" },
];

function Shell({ children }: { children: ReactNode }) {
  const { apiStatus, session, isRestoring, signIn, signOut } = useSession();
  const pathname = usePathname();

  if (isRestoring) {
    return <LoadingState label="Restoring your session…" />;
  }

  if (!session) {
    return (
      <main className="auth-shell">
        <AuthPanel onAuthenticated={signIn} />
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>SaaS Intelligence</h1>
          <p className="eyebrow">v1.0</p>
        </div>

        <nav aria-label="Main" className="sidebar-nav">
          {NAV.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : (pathname ?? "").startsWith(item.href);
            return (
              <Link key={item.href} aria-current={active ? "page" : undefined} href={item.href}>
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className="api-status-mini">
            API: <span className="status-dot" data-status={apiStatus} />
          </div>
          <span className="nav-user">{session.user.email}</span>
          <button onClick={signOut} type="button">
            Sign out
          </button>
        </div>
      </aside>

      <main className="content-area">
        {children}
      </main>
    </div>
  );
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
