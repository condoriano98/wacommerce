"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";
import { rupiah } from "@/lib/api";

export default function Approvals() {
  const { result, query } = useList<any>({ resource: "approvals", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Approvals" error={query.error?.message}>
      <p className="text-sm text-slate-500">The requester can never approve their own request.</p>
      <Card><Table rows={result?.data ?? []} columns={[
        { key: "kind", label: "Kind" }, { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
        { key: "amount", label: "Amount", render: (r) => rupiah(r.amount) }, { key: "subject", label: "Subject" }]} /></Card>
    </Page>
  );
}
