import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AppProviders } from "../components/app-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Scraper Automation System",
  description: "Monitor product prices and stock across sources.",
};

export const viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <AppProviders>{children}</AppProviders>
      </body>
    </html>
  );
}
