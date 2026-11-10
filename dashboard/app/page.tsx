"use client";
import { Card, Stat } from "@/components/ui/card";
import { Page } from "@/components/Page";
import { useOms } from "@/components/useOms";
import { rupiah } from "@/lib/api";

type Wf = Record<string, string>;
type Forecast = { minimum_closing_cash: string; opening_cash: string; excluded_unverified_transfers: string };

export default function Overview() {
  const wf = useOms<Wf>("/fpa/waterfall");
  const cash = useOms<Forecast>("/fpa/cash-forecast");
  const ex = useOms<{ severity: string; exposure: string | null }[]>("/exceptions");
  const exposure = (ex.data ?? []).reduce((s, c) => s + Number(c.exposure ?? 0), 0);
  return (
    <Page title="Overview" error={wf.error ?? cash.error ?? ex.error}>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <Card testId="kpi-net-sales"><Stat label="Net sales (month)" value={rupiah(wf.data?.net_sales)} /></Card>
        <Card testId="kpi-cm3"><Stat label="CM3 (month)" value={rupiah(wf.data?.cm3)}
          tone={Number(wf.data?.cm3 ?? 0) < 0 ? "bad" : "good"} /></Card>
        <Card testId="kpi-min-cash"><Stat label="13-week minimum cash" value={rupiah(cash.data?.minimum_closing_cash)} /></Card>
        <Card testId="kpi-exposure"><Stat label="Open exception exposure" value={rupiah(exposure)}
          tone={exposure > 0 ? "bad" : undefined} /></Card>
      </div>
      <Card title="Unverified transfers excluded from cash">
        <p className="text-sm">{rupiah(cash.data?.excluded_unverified_transfers)} — counted only after a bank match.</p>
      </Card>
    </Page>
  );
}
