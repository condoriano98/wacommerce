import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, cookieOptions, isExpired } from "@/lib/session";

// Route guard (Next.js 16 "proxy" convention): every page except sign-in requires a live session. An expired
// session is cleared and the user is sent back to sign-in. API routes answer for themselves (401/403 JSON).
export function proxy(req: NextRequest) {
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (token && !isExpired(token)) return NextResponse.next();
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = token ? "?expired=1" : `?next=${encodeURIComponent(req.nextUrl.pathname)}`;
  const res = NextResponse.redirect(url);
  if (token) res.cookies.set(SESSION_COOKIE, "", cookieOptions(0));
  return res;
}

export const config = { matcher: ["/((?!login|api/|_next/|favicon\\.ico).*)"] };
