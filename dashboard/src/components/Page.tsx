"use client";
import type { ReactNode } from "react";

export function Page({ title, children, error }: { title: string; children: ReactNode; error?: string | null }) {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">{title}</h1>
      {error && <div role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800">{error}</div>}
      {children}
    </div>
  );
}
