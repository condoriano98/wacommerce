import { NextResponse, type NextRequest } from "next/server";
import { CSRF_HEADER, SESSION_COOKIE, cookieOptions, isExpired, omsApiUrl } from "@/lib/session";

export const dynamic = "force-dynamic";

// Same-origin proxy to the OMS API (remediation E1). The bearer token is attached here, server-side, from the
// httpOnly session cookie. Only API reads/commands go through: sign-in has its own route and provider webhooks are
// never reachable from the browser. Every request must carry the dashboard header (CSRF defence on top of
// SameSite=Strict); the OMS still authorises every call (permissions + PostgreSQL RLS).
const BLOCKED = /^(auth|webhooks)(\/|$)/;

function expired() {
  const res = NextResponse.json({ detail: "session expired" }, { status: 401 });
  res.cookies.set(SESSION_COOKIE, "", cookieOptions(0));
  return res;
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  if (path.some((seg) => seg === "." || seg === ".." || seg === "")) {
    return NextResponse.json({ detail: "invalid path" }, { status: 400 });
  }
  const target = path.map(encodeURIComponent).join("/");
  if (BLOCKED.test(target)) return NextResponse.json({ detail: "not available through the dashboard" }, { status: 404 });
  if (req.headers.get(CSRF_HEADER) !== "1") return NextResponse.json({ detail: "missing dashboard header" }, { status: 403 });
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (!token || isExpired(token)) return expired();
  const init: RequestInit = {
    method: req.method, cache: "no-store",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": req.headers.get("content-type") ?? "application/json" },
  };
  if (!["GET", "HEAD"].includes(req.method)) init.body = await req.text();
  let upstream: Response;
  try {
    upstream = await fetch(`${omsApiUrl()}/${target}${req.nextUrl.search}`, init);
  } catch {
    return NextResponse.json({ detail: "OMS unavailable" }, { status: 502 });
  }
  if (upstream.status === 401) return expired();
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
  });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
