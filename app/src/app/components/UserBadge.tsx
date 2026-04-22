"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useAuth } from "../lib/authContext";

export default function UserBadge() {
  const { user, logout } = useAuth();
  const router = useRouter();

  if (user === null) return null;

  function handleLogout() {
    logout();
    router.replace("/login");
  }

  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span className="font-mono">@{user.username}</span>
      <button
        type="button"
        onClick={handleLogout}
        className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-slate-600 transition-colors hover:border-[color:var(--color-petrol)]/40 hover:text-[color:var(--color-petrol)]"
      >
        <LogOut size={12} />
        Logout
      </button>
    </div>
  );
}
