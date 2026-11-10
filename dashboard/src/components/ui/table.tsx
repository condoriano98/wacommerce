import type { ReactNode } from "react";

export type Column<T> = { key: string; label: string; render?: (row: T) => ReactNode };

export function Table<T extends Record<string, unknown>>({ rows, columns, empty = "Nothing to show" }: {
  rows: T[]; columns: Column<T>[]; empty?: string;
}) {
  if (!rows.length) return <p className="text-sm text-slate-500">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs uppercase text-slate-500">
            {columns.map((c) => <th key={c.key} className="px-2 py-2">{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b last:border-0">
              {columns.map((c) => <td key={c.key} className="px-2 py-2">{c.render ? c.render(r) : String(r[c.key] ?? "–")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
