export interface AuthUser {
  id: number;
  username: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

function baseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
}

export class AuthError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface FastAPIValidationError {
  loc?: unknown[];
  msg?: string;
}

async function parseError(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    // 401/409 etc: `detail` is a plain string we raised via HTTPException.
    if (typeof data.detail === "string") return data.detail;
    // 422: `detail` is a list of Pydantic validation errors. Surface the
    // first field + message so the user sees something actionable instead
    // of the generic fallback.
    if (Array.isArray(data.detail) && data.detail.length > 0) {
      const first = data.detail[0] as FastAPIValidationError;
      const field = Array.isArray(first.loc)
        ? String(first.loc[first.loc.length - 1] ?? "input")
        : "input";
      const msg = typeof first.msg === "string" ? first.msg : fallback;
      return `${field}: ${msg}`;
    }
  } catch {
    // Non-JSON body — fall through to the fallback message.
  }
  return fallback;
}

export async function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  const res = await fetch(`${baseUrl()}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    throw new AuthError(
      await parseError(res, "Invalid credentials"),
      res.status,
    );
  }
  return (await res.json()) as LoginResponse;
}

export async function register(
  username: string,
  password: string,
): Promise<AuthUser> {
  const res = await fetch(`${baseUrl()}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    throw new AuthError(
      await parseError(res, "Registration failed"),
      res.status,
    );
  }
  return (await res.json()) as AuthUser;
}

export async function me(token: string): Promise<AuthUser> {
  const res = await fetch(`${baseUrl()}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new AuthError(await parseError(res, "Session expired"), res.status);
  }
  return (await res.json()) as AuthUser;
}
