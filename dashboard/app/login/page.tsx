"use client";
import { useState } from "react";
import { Page } from "@/components/Page";
import { omsFetch, setToken } from "@/lib/api";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await omsFetch("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      setToken(r.access_token); setMsg("Signed in");
    } catch (err: any) { setToken(null); setMsg(err.message ?? "Sign-in failed"); }
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
