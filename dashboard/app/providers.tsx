"use client";
import { Refine } from "@refinedev/core";
import routerProvider from "@refinedev/nextjs-router";
import type { ReactNode } from "react";
import { dataProvider } from "@/lib/dataProvider";

const resources = [
  { name: "portfolios", list: "/portfolios" }, { name: "orders", list: "/orders" },
  { name: "procurement/tasks", list: "/procurement" }, { name: "approvals", list: "/approvals" },
  { name: "exceptions", list: "/exceptions" }, { name: "integrations/capabilities", list: "/integrations" },
];

export function Providers({ children }: { children: ReactNode }) {
  return (
    <Refine dataProvider={dataProvider} routerProvider={routerProvider} resources={resources}
            options={{ disableTelemetry: true, syncWithLocation: false }}>
      {children}
    </Refine>
  );
}
