import type { Metadata } from "next";
import Link from "next/link";
import { Suspense, type ReactNode } from "react";
import "./globals.css";
import { SignOut } from "@/components/SignOut";
import { Providers } from "./providers";

export const metadata: Metadata = { title: "Wacommerce OMS", description: "Operations, finance and capital" };

const nav = [
  ["/", "Overview"], ["/portfolios", "Portfolios"], ["/orders", "Orders"], ["/procurement", "Procurement"],
  ["/approvals", "Approvals"], ["/exceptions", "Exceptions"], ["/fpa", "FP&A waterfall"], ["/capital", "Capital"],
  ["/simulator", "Simulator"], ["/integrations", "Integrations"],
];

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Suspense fallback={null}>
        <Providers>
          <div className="flex min-h-screen">
            <nav aria-label="Main" className="w-56 shrink-0 border-r border-slate-200 bg-white p-4">
              <div className="mb-6 font-bold">Wacommerce OMS</div>
              <ul className="space-y-1 text-sm">
                {nav.map(([href, label]) => (
                  <li key={href}><Link className="block rounded px-2 py-1 hover:bg-slate-100" href={href}>{label}</Link></li>
                ))}
              </ul>
              <SignOut />
              <p className="mt-6 text-xs text-slate-400">All providers mocked. No live actions.</p>
            </nav>
            <main className="flex-1 p-6">{children}</main>
          </div>
        </Providers>
        </Suspense>
      </body>
    </html>
  );
}
