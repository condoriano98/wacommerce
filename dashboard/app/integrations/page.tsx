"use client";
import { useList } from "@refinedev/core";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Page } from "@/components/Page";
import { noRetryOn4xx } from "@/components/useOms";

export default function Integrations() {
  const { result, query } = useList<any>({ resource: "integrations/capabilities", queryOptions: { retry: noRetryOn4xx } });
  return (
    <Page title="Integration health" error={query.error?.message}>
      <p className="text-sm text-slate-500">An adapter in code is not access: only production-verified + available counts as ready.</p>
      <Card><Table rows={result?.data ?? []} columns={[{ key: "provider", label: "Provider" }, { key: "account", label: "Account" },
        { key: "capability", label: "Capability" }, { key: "implementation_status", label: "Implementation" },
        { key: "access_state", label: "Access", render: (r) => <Badge value={r.access_state} /> },
        { key: "operationally_ready", label: "Ready", render: (r) => (r.operationally_ready ? "yes" : "no") }]} /></Card>
    </Page>
  );
}
