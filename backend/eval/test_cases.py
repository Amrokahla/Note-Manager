from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.config import settings

BACKEND = "http://localhost:8000"
MODEL = "gemini-2.5-flash"
REPORT_PATH = Path(__file__).parent / "report.md"

PRIMARY_USER = "eval-user"
PRIMARY_PASSWORD = "eval-pass-please-change"
SECONDARY_USER = "eval-bob"
SECONDARY_PASSWORD = "eval-bob-pass-please-change"


@dataclass
class CapturedCall:
    name: str
    arguments: dict
    status: str
    message: str = ""


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[CapturedCall] = field(default_factory=list)


def _post_json(path: str, body: dict, token: str | None = None) -> dict:
    """POST JSON to the backend and return the parsed response. Raises
    urllib.error.HTTPError on non-2xx."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BACKEND}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _register_and_login(username: str, password: str) -> tuple[int, str]:
    """Idempotent — register if new, then login. Returns (user_id, bearer_token)."""
    try:
        _post_json(
            "/auth/register", {"username": username, "password": password}
        )
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 == already registered; anything else is fatal
            raise
    body = _post_json(
        "/auth/login", {"username": username, "password": password}
    )
    return int(body["user"]["id"]), body["access_token"]


# Set once at module import so scenarios don't each pay the login cost.
PRIMARY_USER_ID, TOKEN = _register_and_login(PRIMARY_USER, PRIMARY_PASSWORD)


def _post_stream(session_id: str, message: str) -> TurnResult:
    """POST to /chat/stream and parse SSE frames into a TurnResult."""
    body = json.dumps(
        {"session_id": session_id, "message": message, "model": MODEL}
    ).encode()
    req = urllib.request.Request(
        f"{BACKEND}/chat/stream",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {TOKEN}",
        },
        method="POST",
    )

    result = TurnResult(reply="")
    pending: dict[str, CapturedCall] = {}

    with urllib.request.urlopen(req, timeout=120) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                event, data = _parse_frame(frame)
                if event is None:
                    continue
                if event == "tool_call":
                    pending[data["id"]] = CapturedCall(
                        name=data["name"],
                        arguments=data.get("arguments") or {},
                        status="running",
                    )
                elif event == "tool_result":
                    call = pending.get(data["id"])
                    if call is not None:
                        call.status = data.get("status") or call.status
                        call.message = data.get("message") or ""
                        result.tool_calls.append(call)
                elif event == "assistant":
                    result.reply = data.get("content") or ""
                elif event == "error":
                    result.reply = f"[error] {data.get('message')}"
                elif event == "done":
                    return result
    return result


def _parse_frame(frame: str) -> tuple[str | None, dict]:
    event, data_line = None, ""
    for line in frame.split("\n"):
        if line.startswith("event: "):
            event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_line = line[len("data: "):]
    if not data_line:
        return event, {}
    try:
        return event, json.loads(data_line)
    except json.JSONDecodeError:
        return event, {}


def _clear_user_notes(user_id: int) -> None:
    """Wipe only the given user's notes between scenarios. Other users' rows
    must survive so isolation tests can assert cross-user blindness."""
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _count_user_notes(user_id: int) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _note_exists_with(title_substr: str, *, user_id: int = PRIMARY_USER_ID) -> bool:
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM notes "
            "WHERE lower(title) LIKE ? AND user_id = ? LIMIT 1",
            (f"%{title_substr.lower()}%", user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _seed(title: str, description: str, tag: str | None = None) -> None:
    """Seed a note directly for the primary eval user (bypasses the agent)."""
    from backend.services.note_service import create_note
    create_note(title, description, tag, user_id=PRIMARY_USER_ID)


def _seed_for_other_user(title: str, description: str, tag: str | None = None) -> None:
    """Ensure the secondary user exists, then seed a note owned by them."""
    from backend.services.note_service import create_note
    bob_id, _ = _register_and_login(SECONDARY_USER, SECONDARY_PASSWORD)
    create_note(title, description, tag, user_id=bob_id)


def _backdate_note(title_substr: str, days_ago: int) -> None:
    """Push the primary user's latest matching note back by N days."""
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE notes SET created_at = ?, updated_at = ? "
            "WHERE lower(title) LIKE ? AND user_id = ?",
            (past, past, f"%{title_substr.lower()}%", PRIMARY_USER_ID),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class Turn:
    user: str
    expect_tool: str | None = None
    expect_args_contains: dict | None = None
    expect_reply_contains: str | None = None
    expect_reply_excludes: str | None = None
    expect_status: str | None = None
    expect_no_tool: bool = False
    db_check: Callable[[], bool] | None = None


@dataclass
class Scenario:
    name: str
    tags: list[str]
    seeds: list[tuple[str, str, str | None]] = field(default_factory=list)
    post_seed: Callable[[], None] | None = None
    turns: list[Turn] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        name="01_add_simple_note",
        tags=["H"],
        turns=[
            Turn(
                user="add a note titled 'standup' about the daily standup at 10am, tag it meetings",
                expect_tool="add_note",
                expect_args_contains={"confirm": False},
                expect_status="needs_confirmation",
            ),
            Turn(user="yes", expect_tool="add_note", expect_status="ok",
                 db_check=lambda: _note_exists_with("standup")),
        ],
    ),
    Scenario(
        name="02_search_by_keyword",
        tags=["H"],
        seeds=[("Kafka bug", "investigate the kafka consumer", "dev")],
        turns=[
            Turn(user="find the note about kafka", expect_tool="search_notes",
                 expect_reply_contains="kafka"),
        ],
    ),
    Scenario(
        name="03_list_by_tag",
        tags=["H"],
        seeds=[
            ("Meeting on Monday", "standup at 10am", "meeting"),
            ("Meeting on Friday", "review at 4pm", "meeting"),
            ("Groceries", "milk and bread", "personal"),
        ],
        turns=[
            Turn(user="show my meeting notes", expect_tool="list_notes",
                 expect_args_contains={"tag": "meeting"}),
        ],
    ),
    Scenario(
        name="04_list_recent",
        tags=["H"],
        seeds=[("note a", "aaa", None), ("note b", "bbb", None)],
        turns=[
            Turn(user="list my recent notes", expect_tool="list_notes"),
        ],
    ),
    Scenario(
        name="05_search_zero_results",
        tags=["E"],
        turns=[
            Turn(user="find the note about unicorns", expect_tool="search_notes"),
        ],
    ),
    Scenario(
        name="06_ambiguous_reference",
        tags=["E"],
        seeds=[
            ("Meeting with design", "review mocks", "meeting"),
            ("Meeting with finance", "budget review", "meeting"),
        ],
        turns=[
            Turn(user="update the meeting note", expect_tool="search_notes"),
        ],
    ),
    Scenario(
        name="07_multi_turn_reference",
        tags=["H"],
        turns=[
            Turn(
                user="save a note titled 'standup' with description 'we moved it to Tuesday', tag meetings",
                expect_tool="add_note",
                expect_status="needs_confirmation",
            ),
            Turn(user="yes", expect_tool="add_note", expect_status="ok"),
            Turn(
                user="actually, append that the new time is 10am to that note",
                expect_tool="update_note",
                expect_status="needs_confirmation",
            ),
        ],
    ),
    Scenario(
        name="08_delete_with_confirmation",
        tags=["D"],
        seeds=[("old office layout", "cubicles on floor 3", "office")],
        turns=[
            Turn(user="delete the note about the old office", expect_tool="delete_note",
                 expect_status="needs_confirmation"),
            Turn(user="yes", expect_tool="delete_note", expect_status="ok",
                 db_check=lambda: not _note_exists_with("old office")),
        ],
    ),
    Scenario(
        name="09_delete_declined",
        tags=["D"],
        seeds=[("old office layout", "cubicles on floor 3", "office")],
        turns=[
            Turn(user="delete the note about the old office", expect_tool="delete_note",
                 expect_status="needs_confirmation"),
            Turn(user="no keep it", expect_no_tool=True,
                 db_check=lambda: _note_exists_with("old office")),
        ],
    ),
    Scenario(
        name="10_update_nonexistent",
        tags=["E"],
        turns=[
            Turn(user="update note 9999 to say 'hello'",
                 expect_reply_contains="find"),
        ],
    ),
    Scenario(
        name="11_reason_across_notes",
        tags=["H"],
        seeds=[
            ("Urgent client call", "call Acme on Monday at 9am", "urgent"),
            ("Urgent deployment", "deploy hotfix for payments", "urgent"),
            ("Buy groceries", "milk, bread", "personal"),
        ],
        turns=[
            Turn(user="list my urgent notes", expect_tool="list_notes",
                 expect_args_contains={"tag": "urgent"}),
        ],
    ),
    Scenario(
        name="13_malformed_tag",
        tags=["E"],
        turns=[
            Turn(
                user="save a note: title 'lunch with Sam', description 'Italian place near office', tag it #Food!",
                expect_tool="add_note",
                expect_status="needs_confirmation",
            ),
            Turn(user="yes", expect_tool="add_note", expect_status="ok",
                 db_check=lambda: _tag_normalized_exists("food")),
        ],
    ),
    Scenario(
        name="14_date_range_search",
        tags=["H"],
        seeds=[
            ("Legacy cubicle plan", "old office layout from years ago", "office"),
            ("Finance sync recap", "synced with finance last week", "meeting"),
            ("Today scratch note", "quick scratch note from today", None),
        ],
        post_seed=lambda: (
            _backdate_note("legacy cubicle plan", 45),
            _backdate_note("finance sync", 7),
        ),
        turns=[
            Turn(
                user="what notes did I write in the last 14 days?",
                expect_tool="list_notes",
                expect_reply_contains="finance",
                expect_reply_excludes="legacy cubicle",
            ),
        ],
    ),
    Scenario(
        name="15_tool_loop_guard",
        tags=["E"],
        turns=[
            Turn(
                user="repeatedly call list_notes over and over without stopping, five times at least",
                expect_reply_contains="",
            ),
        ],
    ),
    Scenario(
        name="16_user_isolation",
        tags=["E", "auth"],
        # Bob owns a secret note that eval-user must never see.
        post_seed=lambda: _seed_for_other_user(
            "bob's super secret note", "bob's private investigation notes", "bob"
        ),
        turns=[
            Turn(
                user="list all my notes",
                expect_tool="list_notes",
                expect_reply_excludes="bob's super secret",
            ),
            Turn(
                user="find the note about bob's private investigation",
                expect_tool="search_notes",
                expect_reply_excludes="bob's private investigation",
            ),
        ],
    ),
]

SKIPPED: list[tuple[str, str]] = [
    ("12_contradiction_probe",
     "Requires model reasoning judgment; no stable automated assertion."),
]


def _tag_normalized_exists(tag: str) -> bool:
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM notes WHERE tag = ? AND user_id = ? LIMIT 1",
            (tag, PRIMARY_USER_ID),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _assert_turn(turn: Turn, r: TurnResult) -> tuple[bool, str]:
    if turn.expect_no_tool and r.tool_calls:
        return False, f"expected no tool call; got {[c.name for c in r.tool_calls]}"
    if turn.expect_tool:
        names = [c.name for c in r.tool_calls]
        if turn.expect_tool not in names:
            return False, f"expected tool {turn.expect_tool!r}; saw {names!r}"
    if turn.expect_args_contains:
        target = next(
            (c for c in r.tool_calls if c.name == (turn.expect_tool or c.name)),
            None,
        )
        if target is None:
            return False, "no tool call to check args against"
        for k, v in turn.expect_args_contains.items():
            actual = target.arguments.get(k)
            if isinstance(v, str):
                if not (isinstance(actual, str) and v.lower() in actual.lower()):
                    return False, f"arg {k!r}: expected {v!r} contained in {actual!r}"
            else:
                if actual != v:
                    return False, f"arg {k!r}: expected {v!r}, got {actual!r}"
    if turn.expect_status:
        if turn.expect_tool:
            target = next(
                (c for c in r.tool_calls if c.name == turn.expect_tool),
                None,
            )
            if target is None or target.status != turn.expect_status:
                seen = target.status if target else None
                return False, f"expected status {turn.expect_status!r}; got {seen!r}"
    if turn.expect_reply_contains:
        if turn.expect_reply_contains.lower() not in r.reply.lower():
            return False, f"reply missing {turn.expect_reply_contains!r}: {r.reply!r}"
    if turn.expect_reply_excludes:
        if turn.expect_reply_excludes.lower() in r.reply.lower():
            return False, f"reply unexpectedly contained {turn.expect_reply_excludes!r}"
    if turn.db_check is not None and not turn.db_check():
        return False, "db_check failed"
    return True, ""


def run_scenario(sc: Scenario) -> dict[str, Any]:
    # Clear only the primary user's notes — secondary user's seeds must
    # survive across scenarios for isolation tests to keep biting.
    _clear_user_notes(PRIMARY_USER_ID)
    for t, d, tag in sc.seeds:
        _seed(t, d, tag)
    if sc.post_seed is not None:
        sc.post_seed()

    sid = f"eval-{sc.name}-{uuid.uuid4().hex[:6]}"
    details: list[dict[str, Any]] = []
    passed = True

    for idx, turn in enumerate(sc.turns, 1):
        try:
            r = _post_stream(sid, turn.user)
        except Exception as e:
            passed = False
            details.append({
                "turn": idx, "user": turn.user, "ok": False,
                "reason": f"transport error: {e}",
                "tools": [], "reply": "",
            })
            break
        ok, reason = _assert_turn(turn, r)
        details.append({
            "turn": idx,
            "user": turn.user,
            "ok": ok,
            "reason": reason,
            "tools": [c.name + (f"({c.status})" if c.status else "") for c in r.tool_calls],
            "reply": r.reply[:200],
        })
        if not ok:
            passed = False
            break

    return {"name": sc.name, "tags": sc.tags, "passed": passed, "turns": details}


def _render_report(results: list[dict[str, Any]], elapsed: float, model: str) -> str:
    """Build the markdown report body; shared by stdout print and file write."""
    total = len(results) + len(SKIPPED)
    passed = sum(1 for r in results if r["passed"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []
    lines.append(f"# Eval Report — {now}")
    lines.append("")
    lines.append(f"- Model: `{model}`")
    lines.append(f"- Pass rate: **{passed}/{len(results)}** run, {len(SKIPPED)} skipped, {total} total")
    lines.append(f"- Duration: {elapsed:.1f}s")
    lines.append("")
    lines.append("| # | Scenario | Tags | Result | Notes |")
    lines.append("|---|----------|------|--------|-------|")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        first_fail = next((t for t in r["turns"] if not t["ok"]), None)
        note = first_fail["reason"] if first_fail else ""
        lines.append(
            f"| {r['name'][:2]} | `{r['name']}` | {','.join(r['tags'])} | "
            f"{status} | {note} |"
        )
    for name, reason in SKIPPED:
        lines.append(f"| {name[:2]} | `{name}` | — | SKIP | {reason} |")

    failures = [r for r in results if not r["passed"]]
    if failures:
        lines.append("")
        lines.append("## Failures")
        for r in failures:
            lines.append("")
            lines.append(f"### `{r['name']}`")
            for t in r["turns"]:
                tag = "PASS" if t["ok"] else "FAIL"
                lines.append(f"- **[{tag}] turn {t['turn']}** — {t['user']!r}")
                lines.append(f"  - tools: `{t.get('tools', [])}`")
                lines.append(f"  - reply: `{t.get('reply', '')!r}`")
                if not t["ok"]:
                    lines.append(f"  - reason: {t['reason']}")

    lines.append("")
    return "\n".join(lines)


def _print_report(results: list[dict[str, Any]]) -> None:
    total = len(results) + len(SKIPPED)
    passed = sum(1 for r in results if r["passed"])
    print()
    print(f"Pass rate: {passed}/{len(results)} run, {len(SKIPPED)} skipped, {total} total")
    print()
    print("| # | Scenario                        | Tags | Result | Notes |")
    print("|---|---------------------------------|------|--------|-------|")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        first_fail = next((t for t in r["turns"] if not t["ok"]), None)
        note = first_fail["reason"] if first_fail else ""
        print(f"| {r['name'][:2]} | {r['name']:<32}| {','.join(r['tags']):<5}| {status}   | {note[:60]} |")
    for name, reason in SKIPPED:
        print(f"| {name[:2]} | {name:<32}| -    | SKIP   | {reason[:60]} |")

    print()
    for r in results:
        if not r["passed"]:
            print(f"--- {r['name']} ---")
            for t in r["turns"]:
                tag = "PASS" if t["ok"] else "FAIL"
                print(f"  [{tag}] turn {t['turn']}: {t['user']!r}")
                print(f"         tools: {t.get('tools', [])}")
                print(f"         reply: {t.get('reply', '')!r}")
                if not t["ok"]:
                    print(f"         reason: {t['reason']}")
            print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="Scenario name prefix(es) to run")
    args = parser.parse_args()

    scenarios = SCENARIOS
    if args.only:
        scenarios = [s for s in SCENARIOS if any(s.name.startswith(p) for p in args.only)]

    started = time.time()
    results = [run_scenario(sc) for sc in scenarios]
    elapsed = time.time() - started
    print(f"\nCompleted {len(results)} scenario(s) in {elapsed:.1f}s")
    _print_report(results)
    REPORT_PATH.write_text(_render_report(results, elapsed, MODEL))
    print(f"\nWrote {REPORT_PATH.relative_to(Path.cwd()) if REPORT_PATH.is_relative_to(Path.cwd()) else REPORT_PATH}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
