"use client";
import { signOut } from "@/lib/api";

export function SignOut() {
  return (
    <button type="button" className="mt-4 block rounded px-2 py-1 text-left text-sm hover:bg-slate-100"
            onClick={async () => { await signOut(); window.location.assign("/login"); }}>
      Sign out
    </button>
  );
}
