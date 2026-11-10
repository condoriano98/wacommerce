"use client";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Page } from "@/components/Page";
import { useOms } from "@/components/useOms";
import { rupiah } from "@/lib/api";

export default function Capital() {
  const { data, error } = useOms<any[]>("/capital/summary");
  return (
    <Page title="Capital mandates" error={error}>
      <p className="text-sm text-slate-500">Mandates are management budgets, not cash. Reallocation needs an owner approval.</p>
      <Card><Table rows={data ?? []} columns={[{ key: "code", label: "Portfolio" },
        { key: "total_mandate", label: "Total mandate", render: (r) => rupiah(r.total_mandate) },
        { key: "categories", label: "Available by category", render: (r) => Object.entries(r.categories as Record<string, any>)
          .map(([k, v]) => `${k}: ${rupiah(v.available)}`).join(" · ") || "–" }]} /></Card>
    </Page>
  );
}
