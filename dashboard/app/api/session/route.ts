import { NextResponse, type NextRequest } from "next/server";
import { CSRF_HEADER, SESSION_COOKIE, cookieOptions, isExpired, omsApiUrl, tokenExpiry } from "@/lib/session";

export const dynamic = "force-dynamic";

// POST: sign in. The browser sends credentials to this origin; the server exchanges them with the OMS and keeps the
// token in an httpOnly cookie. DELETE: sign out. GET: whether a (not yet expired) session exists.
export async function POST(req: NextRequest) {
  if (req.headers.get(CSRF_HEADER) !== "1") return NextResponse.json({ detail: "missing dashboard header" }, { status: 403 });
  let body: { email?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "invalid request" }, { status: 400 });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${omsApiUrl()}/auth/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
      body: JSON.stringify({ email: body.email ?? "", password: body.password ?? "" }),
    });
  } catch {
    return NextResponse.json({ detail: "OMS unavailable" }, { status: 502 });
  }
  if (!upstream.ok) return NextResponse.json({ detail: "invalid credentials" }, { status: 401 });
  const { access_token: token } = await upstream.json();
  const exp = tokenExpiry(token);
  if (!token || exp === null) return NextResponse.json({ detail: "invalid token from OMS" }, { status: 502 });
  const res = NextResponse.json({ authenticated: true, expires_at: exp });
  res.cookies.set(SESSION_COOKIE, token, cookieOptions(exp - Date.now() / 1000));
  return res;
}

export async function DELETE(req: NextRequest) {
  if (req.headers.get(CSRF_HEADER) !== "1") return NextResponse.json({ detail: "missing dashboard header" }, { status: 403 });
  const res = NextResponse.json({ authenticated: false });
  res.cookies.set(SESSION_COOKIE, "", cookieOptions(0));
  return res;
}

export async function GET(req: NextRequest) {
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  return NextResponse.json({ authenticated: !!token && !isExpired(token), expires_at: tokenExpiry(token) });
}
