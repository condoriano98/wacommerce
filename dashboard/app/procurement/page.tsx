"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";
import { rupiah } from "@/lib/api";

export default function Procurement() {
  const { result, query } = useList<any>({ resource: "procurement/tasks", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Procurement copilot" error={query.error?.message}>
      <p className="text-sm text-slate-500">Purchases are made by a person in their own marketplace account; the OMS
        records observations, approvals and evidence only.</p>
      <Card><Table rows={result?.data ?? []} columns={[
        { key: "marketplace", label: "Marketplace" }, { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
        { key: "landed_cost", label: "Landed cost", render: (r) => rupiah(r.landed_cost) },
        { key: "expected_contribution", label: "Expected contribution", render: (r) => rupiah(r.expected_contribution) }]} /></Card>
    </Page>
  );
}
