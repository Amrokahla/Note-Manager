---
name: 05-authentication
description: Multi-user mode — username/password registration and login, JWT bearer tokens, per-user data isolation, compound session keying, and the frontend auth UI.
---

# 05 - Authentication

The Note Agent runs in **multi-user mode**: every note is owned by exactly one user, the chat agent always operates inside the authenticated user's scope, and no user can see or modify another user's notes. This document explains how that guarantee is built end-to-end — the data model, the auth layer, the `user_id` threading through the agent stack, and the frontend UX.

The design intentionally stays small. No OAuth, no email verification, no roles, no admin tools — just password-based auth with JWT session tokens and a single `users` table. Everything extra-curricular is marked as a v2 concern.

## High-Level Flow

```
┌────────────────────────────────────────────────────────────────────┐
│                       FRONTEND (Next.js)                            │
│                                                                     │
│   /register  ──────► POST /auth/register                            │
│   /login     ──────► POST /auth/login  ──► { access_token, user }  │
│                          │                                          │
│                          ▼                                          │
│                    localStorage                                     │
│                    (note-agent:auth-token)                          │
│                                                                     │
│   /  AuthGate ──► useAuth().status === "authed"  → renders chat    │
│                   else → router.replace("/login")                   │
│                                                                     │
│   Chat stream  ──► Authorization: Bearer <jwt>                     │
│   401 mid-chat ──► clearToken() + redirect to /login                │
└────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│                       HTTP (FastAPI)                                │
│                                                                     │
│   /auth/register    create_user(username, password)                 │
│   /auth/login       authenticate → tokens.sign(user_id, username)   │
│   /auth/me          current_user dependency → UserPublic            │
│                                                                     │
│   /chat  /chat/stream  /models                                      │
│     └── Depends(current_user) resolves JWT → UserPublic             │
│         └── handle_user_message(session_id, text, user_id=user.id) │
└────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│                     AGENT / TOOLS / DB                              │
│                                                                     │
│   SessionStore.get(session_id, user_id=...)   compound key          │
│   note_tools.execute(name, args, user_id=...) keyword-only          │
│   note_service.*(..., user_id=...)                                  │
│     └── every SQL touching `notes` has `WHERE user_id = ?`          │
└────────────────────────────────────────────────────────────────────┘
```

No change to the provider dispatcher, tool schemas, or `ToolResult` envelope. Auth threads in **orthogonally** to the existing layer model — every pre-auth invariant stays intact.

## Data Model

### `users` table

```sql
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
```

Usernames, not emails. That keeps v1 onboarding frictionless (no SMTP, no verification flow). Email can be added as an optional profile field later.

The explicit `idx_users_username` is redundant with the UNIQUE constraint's auto-index but survives schema introspection cleanly and documents the hot read path (login).

### `notes` gets `user_id`

```sql
ALTER TABLE notes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
DROP INDEX IF EXISTS idx_notes_updated_at;
CREATE INDEX IF NOT EXISTS idx_notes_user_id_updated_at
  ON notes(user_id, updated_at DESC);
```

The composite `(user_id, updated_at DESC)` index serves the common listing read (`SELECT ... WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?`) with a single index seek + scan. The old `idx_notes_updated_at` becomes redundant under multi-tenancy and gets dropped.

SQLite doesn't allow adding a `REFERENCES` constraint via `ALTER TABLE`, so the `user_id` column has no FK declaration in the schema. Enforcement lives in the service layer: every read, update, and delete filters by `user_id`.

### Migration runner

The project had no migration framework before v2. Instead of pulling in Alembic we added a ~40-line runner in `backend/db/migrations.py`:

```python
MIGRATIONS: list[tuple[int, str]] = [
    (1, "<pre-auth schema>"),
    (2, "<users table + notes.user_id + composite index>"),
]

def run_migrations() -> None:
    with tx() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {v for (v,) in conn.execute("SELECT version FROM schema_version")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
```

Idempotent. Versioned. Adding a migration is a one-line append to the list.

Called once from `main.py`'s lifespan. Pre-existing notes (from before v2) get `user_id = 0` — orphaned until the operator either claims them for a real user or drops `data/notes.db` on first multi-user boot.

## Password Hashing

`backend/auth/passwords.py` wraps the `bcrypt` library so no other module calls bcrypt directly:

```python
def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.auth_bcrypt_cost)
    return bcrypt.hashpw(plain.encode(), salt).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False   # malformed hash → never auth
```

**Cost factor 12** (default) is ≈ 250 ms per hash on a laptop — a comfortable balance between CPU and brute-force resistance. Tunable via `AUTH_BCRYPT_COST` for testing and for ops who want a heavier setting.

**Unicode-safe.** The round-trip unit tests assert `pä$$wørd🔑` verifies correctly after hashing; bcrypt handles utf-8-encoded input fine up to 72 bytes.

## JWT Tokens

`backend/auth/tokens.py` uses PyJWT with HS256 (symmetric, single-secret). Simpler than RS256 for a single-server app.

```python
def sign(user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.auth_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, _require_secret(), algorithm="HS256")
```

**Payload**: `sub` (user id as string), `username`, `iat`, `exp`. Nothing sensitive — the username leaks only to whoever already has the token.

**TTL**: default 7 days (`AUTH_TOKEN_TTL_MINUTES=10080`). Short enough to limit damage from a stolen token, long enough to avoid constant re-login.

**No fallback secret.** `_require_secret()` raises `RuntimeError` if `settings.auth_secret` is empty. This is backed up by a hard check in the FastAPI lifespan:

```python
if not settings.auth_secret:
    raise RuntimeError(
        "AUTH_SECRET is not set. Refusing to start — a silent default "
        "would sign every JWT with a known secret."
    )
```

A silent default would be a catastrophic production footgun, so the app refuses to boot without one.

## HTTP Surface

| Method | Path              | Body / headers                           | Response                                         | Errors        |
|--------|-------------------|------------------------------------------|--------------------------------------------------|---------------|
| POST   | `/auth/register`  | `{ username, password }`                 | 201 `UserPublic`                                 | 422, 409      |
| POST   | `/auth/login`     | `{ username, password }`                 | 200 `{ access_token, token_type, expires_in, user }` | 401           |
| GET    | `/auth/me`        | `Authorization: Bearer <jwt>`            | 200 `UserPublic`                                 | 401           |
| POST   | `/chat`           | `Bearer <jwt>` + `ChatIn` body           | 200 `ChatOut`                                    | 401, 422      |
| POST   | `/chat/stream`    | `Bearer <jwt>` + `ChatIn` body           | 200 SSE                                          | 401, 422      |
| GET    | `/models`         | `Bearer <jwt>`                           | 200 model list                                   | 401           |

**Register does not auto-login.** Separate endpoints, predictable flow, matches OAuth-style patterns users already expect. The frontend composes register → login into one UI action via `AuthProvider.register(...)`.

**Login error messages are deliberately generic** (`"Invalid credentials"`). Both wrong-username and wrong-password return the same 401 to avoid user enumeration.

**CORS** stays `allow_credentials=False` — bearer tokens don't need credentialed CORS. If a future version moves to httpOnly cookies, this flips.

### `current_user` dependency

`backend/auth/dependencies.py`:

```python
_scheme = HTTPBearer(auto_error=False)

def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> UserPublic:
    if creds is None:
        raise HTTPException(401, "Missing bearer token")
    try:
        payload = tokens.verify(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    user = auth_service.get_by_id(int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "User no longer exists")
    return user
```

`auto_error=False` lets the dependency emit a consistent 401 shape instead of FastAPI's default 403 on a missing header.

## `user_id` Threading

Every note-touching function takes `user_id` as a **keyword-only** argument. Keyword-only prevents positional ambiguity as signatures evolve and forces explicit call sites.

```python
# backend/services/note_service.py
def create_note(title, description, tag=None, *, user_id: int) -> Note: ...
def get_note(note_id: int, *, user_id: int) -> Note | None: ...
def update_note(note_id, title=None, description=None, tag=None, *,
                clear_tag=False, user_id: int) -> Note | None: ...
def delete_note(note_id: int, *, user_id: int) -> bool: ...
def list_notes(tag=None, limit=10, date_from=None, date_to=None,
               *, user_id: int) -> list[NoteSummary]: ...
def list_tags(limit=4, *, user_id: int) -> list[TagCount]: ...
def search_semantic(query, limit=5, threshold=None, fallback_limit=None,
                    *, user_id: int) -> tuple[list[NoteSummary], bool]: ...

# backend/tools/note_tools.py
def execute(name: str, raw_args: dict | None, *, user_id: int) -> ToolResult: ...

# backend/agent/intent_parser.py
def handle_user_message(session_id, user_text, emit=None, *,
                        user_id: int, model=DEFAULT_MODEL) -> TurnResult: ...
```

**Invariant**: every SQL query that touches `notes` co-filters on `user_id`. You can verify with:

```bash
grep -nE "FROM notes|UPDATE notes|DELETE FROM notes" backend/services/note_service.py
```

Every hit is inside a statement that also has `AND user_id = ?` — with one documented exception, `backfill_embeddings()`, which runs at startup before any user context exists and only writes embedding BLOBs back to rows it already knows the id of. It cannot leak data across users because it touches only the `embedding` column of rows it reads by id.

## Session State — Compound Keying

Before v2, `SessionStore` keyed sessions by `session_id` alone. That would let two users with the same (client-generated UUID) session_id accidentally share pronoun-resolution state. Unlikely in practice but a real correctness bug.

v2 uses a flat compound key:

```python
def _compound_key(user_id: int | None, session_id: str) -> str:
    return f"{user_id if user_id is not None else '-'}:{session_id}"

class SessionStore:
    def get(self, session_id: str, *, user_id: int | None = None) -> SessionState: ...
    def reset(self, session_id: str, *, user_id: int | None = None) -> None: ...
    def reset_user(self, user_id: int) -> None: ...   # drops all of one user's sessions
```

Flat dict beats a nested `dict[user_id, dict[session_id, SessionState]]`:
- O(1) lookup and cleanup either way, but flat is trivial to cap globally later (e.g. bound the total session count across all users).
- `reset(user_id, session_id)` and `reset_user(user_id)` both land as one-liners.

Pronoun-resolution ids (`last_referenced_note_ids`) stay per-session. Adding user isolation doesn't change their shape — they were always local to one conversation.

**Covered by unit tests**: `test_conversation_state.py::test_store_isolates_sessions_by_user_id` proves two users hitting the store with the same literal `session_id` get distinct state.

## Frontend

### File map

```
app/src/app/
├── (auth)/
│   ├── layout.tsx          ← centered-card shell, no Header
│   ├── login/page.tsx
│   └── register/page.tsx
├── lib/
│   ├── authStorage.ts      ← get/set/clear token in localStorage, SSR-safe
│   ├── authApi.ts          ← login / register / me HTTP calls
│   └── authContext.tsx     ← <AuthProvider> + useAuth()
├── components/
│   ├── AuthGate.tsx        ← redirects to /login when no token
│   └── UserBadge.tsx       ← username + logout in the header
└── layout.tsx              ← wraps the whole app in <AuthProvider>
```

The `(auth)` route group uses Next.js grouped routes so login/register don't inherit the chat layout (header + grid). They get a simple centered-card shell instead. The grouping doesn't show in the URL — `/login` and `/register` are the public paths.

### Auth context

```tsx
const { user, status, login, register, logout, refresh } = useAuth();
// status ∈ "idle" | "authed" | "unauthed"
```

On mount, `AuthProvider` reads any stored token and validates it against `/auth/me`. A valid response transitions `status` to `"authed"`; any error (401, network, expired) clears the token and sets `"unauthed"`. That mount-time validation is why `AuthGate` can render a hard authed/unauthed decision without flashing unauthenticated content to an actually-logged-in user.

### AuthGate

```tsx
const { status } = useAuth();
useEffect(() => {
  if (status === "unauthed") router.replace("/login");
}, [status, router]);
if (status !== "authed") return <Loading />;
return children;
```

Wraps the `/` page. Three states:
- `"idle"` — still validating the stored token → show a tiny loading shell.
- `"unauthed"` — bounce to `/login`.
- `"authed"` — render the chat.

### Token storage — localStorage

| Store           | Pros                          | Cons                                              | Decision       |
|-----------------|-------------------------------|---------------------------------------------------|----------------|
| localStorage    | Trivial, cross-tab            | XSS can read                                      | **Chosen v1**  |
| sessionStorage  | Clears on tab close           | Still XSS; inconvenient across tabs               | —              |
| httpOnly cookie | XSS-safe, auto-sent           | CSRF, cookie-domain setup, credentialed CORS      | v2 target      |

v1 uses localStorage. The app has no user-generated HTML rendering path today (all assistant text renders as `whitespace-pre-wrap`, never `dangerouslySetInnerHTML`), so the XSS surface is small. The trade-off is captured here and in `DESIGN.md`; the v2 target is to move the token into an httpOnly cookie.

### `api.ts` header injection + 401 handling

Every `fetch` to `/chat/stream` reads the token from `authStorage` and injects `Authorization: Bearer <token>`. On `res.status === 401`:

```ts
if (res.status === 401) {
  clearToken();
  handlers.onError("Session expired. Please log in again.");
  handlers.onDone();
  window.location.href = "/login";
  return;
}
```

The hard redirect (not `router.replace`) is intentional — this path runs outside React, from a fetch that may have already begun streaming.

### User-facing validation error handling

FastAPI returns Pydantic validation failures as a 422 with `detail: [{ loc, msg, type }, ...]`. The frontend's `parseError` helper unpacks the first entry so the user sees `username: String should match pattern '^[a-zA-Z0-9_.-]+$'` instead of a generic "Registration failed". The register form also mirrors the backend username regex client-side so invalid characters are caught before submit.

## Configuration

```bash
# .env
AUTH_SECRET=<32+ random bytes — generate with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`>
AUTH_TOKEN_TTL_MINUTES=10080     # 7 days
AUTH_BCRYPT_COST=12              # ~250 ms per hash on a laptop
```

**`AUTH_SECRET` has no default.** The app refuses to start without it. `docker-compose.yml` enforces the same rule at the compose layer via `${AUTH_SECRET:?AUTH_SECRET must be set in .env}` — `docker compose up` fails fast if the env var is missing.

## Evaluation Harness

`backend/eval/test_cases.py` handles auth in two places:

1. **At module import**, it registers (idempotent — 409 ignored) and logs in as `eval-user`, storing the bearer token globally. Every subsequent `_post_stream` call attaches that token.
2. **Scenario `16_user_isolation`** additionally registers a secondary user (`eval-bob`) and seeds a note owned by Bob. The scenario then asks eval-user to list / search their own notes and asserts the reply **does not** contain Bob's title. Cross-user blindness becomes a regressable test, not a one-off manual check.

To keep isolation biting across scenarios, `_clear_user_notes(PRIMARY_USER_ID)` replaces the previous global `DELETE FROM notes`. Bob's seed survives cleanup so the isolation assertion stays meaningful turn-to-turn.

## Defensive Choices

| Choice                                             | Rationale                                                              |
|----------------------------------------------------|------------------------------------------------------------------------|
| Usernames, not emails                              | Zero SMTP dependency; defer verification to v2.                        |
| `AUTH_SECRET` has no default                       | Silent defaults sign every JWT with a known key — production footgun.  |
| Token in localStorage (v1)                         | Honest trade-off. v2 target is httpOnly cookie once CSRF is handled.   |
| Hand-rolled migration runner                       | One migration file, no framework, readable in a PR review.             |
| Compound SessionStore key                          | Flat dict, `reset_user()` is one line, trivially testable.             |
| `user_id` keyword-only in every backend signature  | Forces explicit call sites; future-proof as signatures grow.           |
| Auth state outside the chat reducer                | Single-responsibility; the reducer has no opinion about who the user is.|
| No auto-login after `/auth/register`               | Single-purpose endpoints, predictable flow.                            |
| Generic 401 on wrong username or wrong password    | Avoids user enumeration.                                               |
| `bcrypt` cost 12                                   | ≈ 250 ms per hash — well-studied default.                              |

## Out of Scope (v1)

- OAuth / OIDC / social login.
- Email verification and password reset.
- Role-based access (admin vs. user).
- Multi-factor auth.
- Rate limiting (add via middleware later).
- Server-side token revocation / blocklist. v1 relies on expiry.
- Admin UI.

## Relationship to the Rest of the Docs

- `01-architecture.md` — the overall layer model is unchanged; auth threads in orthogonally.
- `02-data-models.md` — `users` table and `UserPublic` / `TokenOut` Pydantic shapes live alongside `Note` / `NoteSummary`.
- `03-note-agent.md` — the orchestrator now takes `user_id` but its decision flow is untouched.
- `04-memory-and-state.md` — `SessionStore` keying changed to compound `(user_id, session_id)`; everything else is identical.
- `TOOLS.md` — tool contracts unchanged; scoping is enforced server-side, never reflected in the arg schemas.
- `DESIGN.md` — the decisions table above is the authentication section of the overall design log.
