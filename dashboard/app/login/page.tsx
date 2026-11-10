"use client";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { Page } from "@/components/Page";
import { signIn } from "@/lib/api";

export default function Login() {
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(
    params.get("expired") ? "Your session has expired. Please sign in again." : null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    try {
      await signIn(email, password);
      const next = params.get("next");
      // Full navigation: the client router may hold prefetched pre-sign-in redirects for protected pages.
      window.location.assign(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (err: any) {
      setMsg(err.message ?? "Sign-in failed");
    }
  }
  return (
    <Page title="Sign in">
      <form onSubmit={submit} className="max-w-sm space-y-3">
        <input aria-label="Email" className="w-full rounded border p-2" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input aria-label="Password" type="password" className="w-full rounded border p-2" value={password}
               onChange={(e) => setPassword(e.target.value)} />
        <button className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white" type="submit">Sign in</button>
        {msg && <p role="status" className="text-sm">{msg}</p>}
      </form>
    </Page>
  );
}
