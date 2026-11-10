const tones: Record<string, string> = {
  critical: "bg-red-100 text-red-800", high: "bg-orange-100 text-orange-800", medium: "bg-amber-100 text-amber-800",
  mocked: "bg-slate-200 text-slate-700", available: "bg-emerald-100 text-emerald-800",
  pending: "bg-sky-100 text-sky-800", approved: "bg-emerald-100 text-emerald-800", rejected: "bg-red-100 text-red-800",
};
export function Badge({ value }: { value: string | null | undefined }) {
  const v = value ?? "–";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tones[v] ?? "bg-slate-100 text-slate-700"}`}>{v}</span>;
}
