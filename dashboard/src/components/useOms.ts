"use client";
import { useCustom } from "@refinedev/core";

// Thin wrapper over Refine's useCustom for GET reads of OMS endpoints. Refine v5 exposes a `{}` placeholder in
// `result.data` while loading, so data is taken from the settled query only. Client errors (4xx) are not retried.
export const noRetryOn4xx = (count: number, err: any) => !(err?.statusCode >= 400 && err?.statusCode < 500) && count < 2;

export function useOms<T = unknown>(url: string, query?: Record<string, string>) {
  const { query: q } = useCustom<any>({ url, method: "get", config: { query }, queryOptions: { retry: noRetryOn4xx } });
  return { data: (q.isSuccess ? q.data?.data : null) as T | null, loading: q.isLoading, error: q.error?.message ?? null };
}
