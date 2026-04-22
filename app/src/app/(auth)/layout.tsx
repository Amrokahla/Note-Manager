import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="mb-4 text-center text-lg font-semibold text-[color:var(--color-petrol)]">
          Note Agent
        </h1>
        {children}
      </div>
    </main>
  );
}
