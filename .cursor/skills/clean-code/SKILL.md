---
name: clean-code
description: Enforces clean-code standards when writing or editing source files — minimal and single-line comments only, consistent indentation, one responsibility per file, no redundant code, small focused functions, meaningful names, and explicit error handling. Use when creating new files, implementing features, refactoring, or whenever the user asks for "clean code", "tidy this up", "refactor", "simplify", "remove duplication", or mentions code quality, readability, or maintainability.
---

# Clean Code

Apply these rules to every file you write or edit. They are non-negotiable unless the user explicitly asks for an exception.

## Core Rules

### 1. Comments — minimal, single-line, intent-only
- **No block comments** (no `/** ... */`, no multi-line `# ...` paragraphs, no banner comments like `# ===== SECTION =====`).
- **One-line comments only** — max ~100 chars, one per comment.
- **Explain intent, not mechanics.** The code shows *what*; the comment explains *why*.
- **No narrating comments** — remove anything restating the next line.
- **No section dividers** (`# ----- Helpers -----`). If you need them, the file is too big — split it.
- **No TODO/FIXME dumps.** If a TODO is worth writing, open a task or file an issue; don't leave archaeology.
- **No author/date/changelog headers.** Git already has this.
- **Docstrings are allowed** for public functions/classes, but keep them to 1–3 lines. No full essays.

| Bad | Good |
|---|---|
| `// Increment the counter` above `i++` | *(delete the comment)* |
| `// This function adds two numbers\n// It takes two arguments\n// Returns their sum` | `// Sums two numbers.` |
| `/** Handles the edge case where... (10 lines) */` | `// Retry once: llama3.2 3B occasionally returns tool calls as raw text.` |

### 2. Indentation & formatting
- **Consistent indentation** per language (4 spaces Python, 2 spaces JS/TS/JSON/YAML). Never mix tabs and spaces.
- **No trailing whitespace**, **final newline** at EOF.
- **Max line length ~100 chars.** Break earlier if it helps readability.
- **One blank line** between functions/methods, **two** between top-level declarations in Python.
- **No commented-out code.** Delete it. Git remembers.
- Let the language's formatter be the law: Ruff/Black for Python, Prettier for JS/TS, `gofmt` for Go.

### 3. One responsibility per file
- **One module, one job.** If a file's purpose takes more than one sentence to describe, split it.
- **No "utils.py" / "helpers.ts" dumping grounds.** Name files by what they do (`tag_normalizer.py`, not `utils.py`).
- **Business logic ≠ I/O ≠ presentation.** Keep layers in separate files even when short.
- **Keep files under ~300 lines** as a soft ceiling. Past that, look for a split.

### 4. No redundant code (keep it minimal)
- **DRY within reason.** If the same logic appears 3+ times, extract it. Twice is a judgment call.
- **Delete dead code** on sight — unreachable branches, unused imports, unused params, orphan functions.
- **No speculative abstractions.** Do not add a parameter/class/interface for a hypothetical future caller.
- **Prefer stdlib and existing deps** before adding a new package.
- **Delete more than you add** when refactoring whenever possible.
- **No re-exports through barrel files** unless the package boundary demands it.

### 5. Small, focused functions
- **≤ 30 lines** per function as a target; hard look above 50.
- **One level of abstraction per function.** Don't mix "parse HTTP body" and "compute business rules" in one body.
- **≤ 4 parameters.** More than that, pass a typed object (dataclass / Pydantic / TypedDict / interface).
- **Return early.** Avoid deep nesting — invert guards.

### 6. Naming
- **Descriptive, not clever.** `remaining_retries` beats `n`.
- **Nouns for variables, verbs for functions.** `active_users`, `find_active_users()`.
- **Booleans read like questions.** `is_ready`, `has_tags`, `can_delete`.
- **No abbreviations** except standard ones (`id`, `url`, `http`). `usr_cnt` → `user_count`.
- **Consistent terminology** across the codebase. Pick one ("note", not "memo"/"entry"/"record") and stick to it.

### 7. Explicit is better than implicit
- **Type every public signature** (Python: hints + `from __future__ import annotations`; TS: no `any`).
- **Validate at the boundary** (Pydantic, Zod, parsers) — then trust types inside.
- **No magic numbers/strings.** Name them: `MAX_TOOL_HOPS = 5`, not `5` inline.
- **No implicit globals.** Pass dependencies in; configure at composition roots.

### 8. Error handling
- **Raise at the source, catch at the boundary.** Don't swallow exceptions in business logic.
- **No bare `except:` / `catch (_) {}`** — always narrow the type or re-raise with context.
- **Return typed error results** at API/tool edges (e.g. `{ok: false, message, error_code}`), not stack traces.
- **Log once, at the catch site.** Don't log *and* re-raise *and* log again up the stack.

### 9. Imports & organization
- **Group and order imports**: stdlib → third-party → local, blank line between groups.
- **Absolute imports** over relative (Python `backend.services.x`, not `..services.x`).
- **No `import *`.** Ever.
- **Remove unused imports.**

### 10. Tests live next to what they test
- Mirror the source tree: `backend/services/note_service.py` ↔ `backend/tests/test_note_service.py`.
- **One behavior per test.** Name it after the behavior: `test_delete_without_confirm_returns_needs_confirmation`.
- **No shared mutable test state.** Fresh DB / in-memory fixture per test.

### 11. Dependencies & side effects
- **No work at import time.** Top-level code creates no DB connections, makes no network calls, spawns no threads.
- **Pure functions by default.** Push side effects (I/O, time, randomness) to the edges so the core is testable.
- **Inject, don't reach.** Pass `settings`, clients, clocks in as arguments; don't import them inside deep helpers.

---

## Workflow — apply this skill

When writing or editing code, internally walk this checklist **before** showing the diff:

```
- [ ] Comments: one-line, intent-only, no blocks, no dividers, no dead TODOs
- [ ] Indentation consistent, no trailing whitespace, final newline present
- [ ] File has exactly one responsibility; name reflects it
- [ ] No duplicated logic; no dead code; no speculative abstractions
- [ ] Functions ≤ ~30 lines, ≤ 4 params, one level of abstraction, early returns
- [ ] Names are descriptive and consistent with the rest of the codebase
- [ ] Types on all public signatures; no magic numbers/strings
- [ ] Errors raised at source, caught at boundary; no bare except
- [ ] Imports grouped, ordered, unused ones removed
- [ ] No side effects at import time
```

If any box fails, fix it before presenting the code.

When **refactoring** existing code:
1. State which rules are being violated.
2. Apply the fix with the smallest reasonable change.
3. Do not rename/reformat unrelated code in the same pass — scope discipline matters too.

---

## Before/After Examples

### Comments

```python
def normalize_tag(t: str) -> str:
    # Import string methods
    # Strip whitespace from both ends
    # Remove leading # if present
    # Lowercase the result
    # Return the cleaned tag
    return t.strip().lstrip("#").lower()
```

Becomes:

```python
def normalize_tag(t: str) -> str:
    return t.strip().lstrip("#").lower()
```

### Redundancy

```python
def add_note(title, body, tags):
    if title is None:
        raise ValueError("title required")
    if title == "":
        raise ValueError("title required")
    if len(title) > 200:
        raise ValueError("title too long")
    ...

def update_note(note_id, title, body, tags):
    if title is None:
        raise ValueError("title required")
    if title == "":
        raise ValueError("title required")
    if len(title) > 200:
        raise ValueError("title too long")
    ...
```

Becomes:

```python
def _validate_title(title: str) -> None:
    if not title or len(title) > 200:
        raise ValueError("title must be 1..200 chars")

def add_note(title: str, body: str, tags: list[str]) -> Note: ...
def update_note(note_id: int, title: str | None, ...) -> Note: ...
```

### Function size & nesting

```python
def handle(msg):
    if msg is not None:
        if msg.get("type") == "tool":
            if msg.get("name") == "add_note":
                if msg.get("args"):
                    # ... 40 more lines
                    ...
```

Becomes:

```python
def handle(msg: dict) -> Reply:
    if msg is None or msg.get("type") != "tool":
        return Reply.ignored()
    name = msg.get("name")
    args = msg.get("args") or {}
    return _dispatch(name, args)
```

### File separation

Bad — `utils.py` holding everything:

```python
# utils.py
def normalize_tag(...): ...
def render_email(...): ...
def cosine(...): ...
def chunk(...): ...
```

Good — one file per concern:

```
tag_normalizer.py
email_renderer.py
similarity.py
iter_helpers.py
```

---

## Language-specific sharpenings

### Python
- `from __future__ import annotations` at the top.
- Pydantic v2 (`BaseModel`, `Field`) for external I/O; dataclasses for internal.
- Prefer `pathlib.Path` over string paths.
- Context managers (`with`) over manual open/close.
- `match` over long `elif` chains where appropriate.

### TypeScript / JavaScript
- Strict mode on. No `any`; use `unknown` + narrowing.
- `const` by default, `let` only when reassigning, never `var`.
- Named exports over default exports unless a framework requires default.
- Prefer `??` over `||` for defaulting on `null`/`undefined`.
- Arrow functions for callbacks, `function` declarations for top-level named functions.

### SQL
- Uppercase keywords (`SELECT`, `FROM`), lowercase identifiers.
- One clause per line for non-trivial queries.
- Parameterised queries always. Never format values into SQL strings.

---

## When to Skip a Rule

Occasionally a rule actively hurts clarity — e.g. a state machine whose 60-line function *is* the clearest form. When skipping a rule:

1. Leave a single-line comment with the reason.
2. Keep the exception local; don't let it spread.

Example:

```python
# Single long function: keeping the state transitions inline reads clearer than 6 tiny helpers.
def run_agent_loop(state: SessionState) -> str:
    ...
```

---

## Self-Check Before Submitting

Ask yourself:

- Can I **delete** anything from this diff without losing meaning?
- Would a new contributor understand each file from its name alone?
- If I removed every comment, would the code still be obvious?
- Does any function do two things? Any file?
- Is anything *almost* a duplicate of something else in the repo?

If the answer to any of these is unsatisfying, iterate before committing.
