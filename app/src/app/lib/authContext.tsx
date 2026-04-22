"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  AuthError,
  login as loginApi,
  me as meApi,
  register as registerApi,
  type AuthUser,
} from "./authApi";
import { clearToken, loadToken, saveToken } from "./authStorage";

type AuthStatus = "idle" | "authed" | "unauthed";

interface AuthValue {
  user: AuthUser | null;
  status: AuthStatus;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
}

const AuthCtx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>("idle");

  const refresh = useCallback(async () => {
    const token = loadToken();
    if (!token) {
      setUser(null);
      setStatus("unauthed");
      return;
    }
    try {
      const u = await meApi(token);
      setUser(u);
      setStatus("authed");
    } catch {
      // Expired or tampered — forget it and drop back to unauthed.
      clearToken();
      setUser(null);
      setStatus("unauthed");
    }
  }, []);

  useEffect(() => {
    // Validate any stored token against the backend once on mount so
    // AuthGate has a definitive authed/unauthed signal before it renders.
    // The setState calls here are driven by network completion, not
    // cascading from render state — we suppress the lint rule for this
    // bounded external-sync pattern.
    let cancelled = false;
    (async () => {
      const token = loadToken();
      if (!token) {
        if (!cancelled) {
          setUser(null);
          setStatus("unauthed");
        }
        return;
      }
      try {
        const u = await meApi(token);
        if (!cancelled) {
          setUser(u);
          setStatus("authed");
        }
      } catch {
        clearToken();
        if (!cancelled) {
          setUser(null);
          setStatus("unauthed");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const { access_token, user: u } = await loginApi(username, password);
    saveToken(access_token);
    setUser(u);
    setStatus("authed");
  }, []);

  const register = useCallback(
    async (username: string, password: string) => {
      await registerApi(username, password);
      // Consumer convenience: register + log in in one UI action. Matches
      // the typical "sign up → land on the app" flow users expect.
      await login(username, password);
    },
    [login],
  );

  const logout = useCallback(() => {
    clearToken();
    setUser(null);
    setStatus("unauthed");
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ user, status, login, register, logout, refresh }),
    [user, status, login, register, logout, refresh],
  );

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthCtx);
  if (ctx === null) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return ctx;
}

export { AuthError };
