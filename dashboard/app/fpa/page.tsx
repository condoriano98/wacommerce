"use client";
import { Card } from "@/components/ui/card";
import { Page } from "@/components/Page";
import { useOms } from "@/components/useOms";
import { rupiah } from "@/lib/api";

const rows: [string, string, boolean][] = [
  ["gross_sales", "Gross sales", false], ["returns_allowances", "Returns & allowances", false],
  ["net_sales", "Net sales", true], ["cm1_costs", "COGS, fees, fulfilment", false], ["cm1", "CM1", true],
  ["marketing", "Attributable marketing", false], ["cm2", "CM2", true], ["cm3_costs", "Returns, AI/CS, credit losses", false],
  ["cm3", "CM3", true], ["opex", "Allocated opex", false], ["operating_profit", "Operating profit", true],
];

export default function Waterfall() {
  const { data, error } = useOms<Record<string, any>>("/fpa/waterfall");
  return (
    <Page title="Contribution waterfall" error={error}>
      <Card title={data ? `Period ${data.period?.[0]} – ${data.period?.[1]}` : "Loading"} testId="waterfall">
        <table className="w-full max-w-xl text-sm">
          <tbody>
            {rows.map(([k, label, total]) => (
              <tr key={k} className={total ? "border-t font-semibold" : ""}>
                <td className="py-1">{label}</td><td className="py-1 text-right" data-testid={`wf-${k}`}>{rupiah(data?.[k])}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data?.unallocated && Object.keys(data.unallocated).length > 0 && (
          <p className="mt-3 text-xs text-slate-500">Unallocated (never spread): {Object.entries(data.unallocated)
            .map(([k, v]) => `${k} ${rupiah(v as string)}`).join(", ")}</p>
        )}
      </Card>
    </Page>
  );
}
