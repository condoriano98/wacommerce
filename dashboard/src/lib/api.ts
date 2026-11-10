// The dashboard reads the OMS API only, and only through its own origin (/api/oms, a server-side proxy that attaches
// the session token from an httpOnly cookie). It never talks to providers or the database (spec §2; remediation E1).
export const API_BASE = "/api/oms";
const HEADERS = { "Content-Type": "application/json", "x-oms-dashboard": "1" };

export type OmsError = { statusCode: number; message: string };

export async function omsFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  for (const [k, v] of Object.entries(HEADERS)) headers.set(k, v);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  } catch {
    throw { statusCode: 0, message: "The dashboard could not reach the server." } satisfies OmsError;
  }
  const body = await res.json().catch(() => null);
  if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
    window.location.assign("/login?expired=1"); // session missing or expired: sign in again
  }
  if (!res.ok) {
    const detail = body?.detail;
    throw { statusCode: res.status, message: typeof detail === "string" ? detail : JSON.stringify(detail ?? body) };
  }
  return body;
}

export async function signIn(email: string, password: string): Promise<void> {
  const res = await fetch("/api/session", { method: "POST", headers: HEADERS, credentials: "same-origin",
                                            body: JSON.stringify({ email, password }) });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw { statusCode: res.status, message: body?.detail ?? "Sign-in failed" } satisfies OmsError;
  }
}

export async function signOut(): Promise<void> {
  await fetch("/api/session", { method: "DELETE", headers: HEADERS, credentials: "same-origin" });
}

export const rupiah = (v: string | number | null | undefined) =>
  v === null || v === undefined ? "–" : `Rp${Number(v).toLocaleString("id-ID", { maximumFractionDigits: 0 })}`;
