"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { AuthError, useAuth } from "../../lib/authContext";

export default function RegisterPage() {
  const { register, status } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (status === "authed") router.replace("/");
  }, [status, router]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    const data = new FormData(e.currentTarget);
    const username = String(data.get("username") ?? "").trim();
    const password = String(data.get("password") ?? "");
    if (username.length < 3) {
      setError("Username must be at least 3 characters.");
      return;
    }
    // Mirror the backend's Pydantic regex so the user gets a clear reason
    // instead of a generic 422 from the API.
    if (!/^[a-zA-Z0-9_.-]+$/.test(username)) {
      setError("Username can only contain letters, digits, and . _ -");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      await register(username, password);
      router.replace("/");
    } catch (err) {
      const msg =
        err instanceof AuthError ? err.message : "Registration failed.";
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
          minLength={3}
          maxLength={40}
          pattern="[a-zA-Z0-9_.\-]+"
          required
          className="mt-1 w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm focus:border-[color:var(--color-petrol)]/40 focus:outline-none"
        />
        <span className="mt-1 block text-[11px] text-slate-400">
          Letters, digits, dot/underscore/hyphen. 3–40 chars.
        </span>
      </label>
      <label className="block text-sm">
        <span className="text-slate-600">Password</span>
        <input
          type="password"
          name="password"
          autoComplete="new-password"
          minLength={8}
          maxLength={200}
          required
          className="mt-1 w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm focus:border-[color:var(--color-petrol)]/40 focus:outline-none"
        />
        <span className="mt-1 block text-[11px] text-slate-400">
          Minimum 8 characters.
        </span>
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
        {busy ? "Creating account…" : "Create account"}
      </button>
      <p className="text-center text-xs text-slate-500">
        Already have an account?{" "}
        <Link href="/login" className="text-[color:var(--color-petrol)] underline">
          Log in
        </Link>
      </p>
    </form>
  );
}
