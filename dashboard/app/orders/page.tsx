"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";
import { rupiah } from "@/lib/api";

export default function Orders() {
  const { result, query } = useList<any>({ resource: "orders", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Orders" error={query.error?.message}>
      <p className="text-sm text-slate-500">Portfolio-limited users see only their own lines (order slices).</p>
      <Card><Table rows={result?.data ?? []} columns={[
        { key: "order_number", label: "Order" }, { key: "channel", label: "Channel" },
        { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
        { key: "lines", label: "Visible lines" }, { key: "units_delivered", label: "Delivered" },
        { key: "visible_line_total", label: "Visible total", render: (r) => rupiah(r.visible_line_total) }]} /></Card>
    </Page>
  );
}
