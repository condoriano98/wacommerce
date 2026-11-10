"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";
import { rupiah } from "@/lib/api";

export default function Exceptions() {
  const { result, query } = useList<any>({ resource: "exceptions", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Reconciliation & exceptions" error={query.error?.message}>
      <Card><Table rows={result?.data ?? []} columns={[
        { key: "type", label: "Type" }, { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} /> },
        { key: "owner_role", label: "Owner" }, { key: "exposure", label: "Exposure", render: (r) => rupiah(r.exposure) },
        { key: "sla_due_at", label: "SLA due" }]} /></Card>
    </Page>
  );
}
