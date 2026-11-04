import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { CONTRACT_ID, NETWORK, accountUrl } from "@/lib/config";

export const metadata: Metadata = {
  title: "KAVREN: agents get paid only for work that passes",
  description: "Acceptance and payment assurance for AI agents hiring AI agents, on NEAR.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <a className="wordmark" href="/">KAVREN</a>
          <Nav />
          <span className="network">NEAR {NETWORK}</span>
        </header>
        <main>{children}</main>
        <footer className="site-footer">
          Contract <a href={accountUrl(CONTRACT_ID)} className="mono">{CONTRACT_ID}</a>. Every job shown here is read
          from the contract by your browser. Testnet only: no real money.
        </footer>
      </body>
    </html>
  );
}
