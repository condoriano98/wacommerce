"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";

export default function Portfolios() {
  const { result, query } = useList<any>({ resource: "portfolios", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Portfolios" error={query.error?.message}>
      <Card><Table rows={result?.data ?? []} columns={[{ key: "code", label: "Code" }, { key: "name", label: "Name" },
        { key: "status", label: "Status" }, { key: "risk_level", label: "Risk" }]} /></Card>
    </Page>
  );
}
