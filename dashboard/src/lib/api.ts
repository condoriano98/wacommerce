// The dashboard reads the OMS API only; it never talks to providers or the database (spec §2).
export const API_URL = process.env.NEXT_PUBLIC_OMS_API_URL ?? "http://localhost:8000";
const KEY = "oms_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(KEY);
}
export function setToken(token: string | null) {
  if (token) window.localStorage.setItem(KEY, token);
  else window.localStorage.removeItem(KEY);
}

export async function omsFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    throw { statusCode: res.status, message: typeof detail === "string" ? detail : JSON.stringify(detail ?? body) };
  }
  return body;
}

export const rupiah = (v: string | number | null | undefined) =>
  v === null || v === undefined ? "–" : `Rp${Number(v).toLocaleString("id-ID", { maximumFractionDigits: 0 })}`;
