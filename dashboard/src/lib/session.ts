// Server-side session helpers (backend-for-frontend, remediation E1). The OMS bearer token lives only in an
// httpOnly, SameSite=Strict cookie; browser JavaScript never sees it, and the browser only ever talks to this origin.
export const SESSION_COOKIE = "oms_session";
export const CSRF_HEADER = "x-oms-dashboard";

/** Base URL of the OMS API, server-side only (never exposed to the browser bundle). */
export function omsApiUrl(): string {
  return (process.env.OMS_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
}

/** Expiry (seconds since epoch) from the JWT payload. Not a signature check: the OMS verifies every request. */
export function tokenExpiry(token: string | undefined): number | null {
  if (!token) return null;
  const part = token.split(".")[1];
  if (!part) return null;
  try {
    const json = JSON.parse(Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
    return typeof json.exp === "number" ? json.exp : null;
  } catch {
    return null;
  }
}

export function isExpired(token: string | undefined, now = Date.now() / 1000): boolean {
  const exp = tokenExpiry(token);
  return exp === null || exp <= now;
}

export function cookieOptions(maxAgeSeconds: number) {
  return {
    httpOnly: true,
    sameSite: "strict" as const,
    // Secure everywhere except an explicitly insecure local setup (http on a non-localhost host).
    secure: process.env.OMS_COOKIE_INSECURE !== "1",
    path: "/",
    maxAge: Math.max(0, Math.floor(maxAgeSeconds)),
  };
}
