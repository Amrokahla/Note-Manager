"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { AuthError, useAuth } from "../../lib/authContext";

export default function LoginPage() {
  const { login, status } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // If a valid token was already in localStorage on mount, refresh() will
  // set status === "authed" — bounce out of the login page.
  useEffect(() => {
    if (status === "authed") router.replace("/");
  }, [status, router]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    const data = new FormData(e.currentTarget);
    const username = String(data.get("username") ?? "").trim();
    const password = String(data.get("password") ?? "");
    if (!username || !password) {
      setError("Fill in both fields.");
      return;
    }
    setBusy(true);
    try {
      await login(username, password);
      router.replace("/");
    } catch (err) {
      const msg = err instanceof AuthError ? err.message : "Login failed.";
      setError(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="space-y-3" onSubmit={handleSubmit}>
      <label className="block text-sm">
        <span className="text-slate-600">Username</span>
        <input
          name="username"
          autoComplete="username"
          required
          className="mt-1 w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm focus:border-[color:var(--color-petrol)]/40 focus:outline-none"
        />
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">Password</span>
        <input
          type="password"
          name="password"
          autoComplete="current-password"
          required
          className="mt-1 w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm focus:border-[color:var(--color-petrol)]/40 focus:outline-none"
        />
      </label>
      {error !== null && (
        <p className="text-xs text-red-600" role="alert">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-md bg-[color:var(--color-petrol)] px-3 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-60"
      >
        {busy ? "Logging in…" : "Log in"}
      </button>
      <p className="text-center text-xs text-slate-500">
        No account?{" "}
        <Link href="/register" className="text-[color:var(--color-petrol)] underline">
          Register
        </Link>
      </p>
    </form>
  );
}
