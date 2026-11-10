"use client";
import { useState } from "react";
import { Card } from "@/components/ui/card";
import { Table } from "@/components/ui/table";
import { Page } from "@/components/Page";
import { omsFetch, rupiah } from "@/lib/api";

export default function Simulator() {
  const [rows, setRows] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true); setError(null);
    try {
      const res = await omsFetch("/fpa/simulations/stress", { method: "POST", body: JSON.stringify({}) });
      setRows(Object.entries(res).map(([name, r]: [string, any]) => ({ name, ...r.consolidated, breaches: r.breaches.length })));
    } catch (e: any) { setError(e.message ?? "failed"); } finally { setBusy(false); }
  }
  return (
    <Page title="Rp100M simulator — stress comparison" error={error}>
      <p className="text-sm text-slate-500">Deterministic, advisory only. Monte Carlo is disabled until distributions are validated.</p>
      <button onClick={run} disabled={busy} className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50">
        {busy ? "Running…" : "Run S1–S9 + combined downside"}</button>
      <Card><Table rows={rows} empty="Run the scenarios to compare." columns={[{ key: "name", label: "Scenario" },
        { key: "cm2", label: "CM2", render: (r) => rupiah(r.cm2) }, { key: "operating_profit", label: "Op. profit", render: (r) => rupiah(r.operating_profit) },
        { key: "min_cash", label: "Min cash", render: (r) => rupiah(r.min_cash) }, { key: "min_cash_week", label: "Week" },
        { key: "breaches", label: "Alerts" }]} /></Card>
    </Page>
  );
}
