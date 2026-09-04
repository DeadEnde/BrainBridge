"""BrainBridge — Model Context Protocol (MCP) server over Google Tasks.

Lets any MCP-capable AI client (Claude Desktop, Claude Code, Cursor, VS Code,
Cline, ...) read/write the user's BrainBridge memory with ZERO repo install:
just point it at the URL

    https://brain-bridge-six.vercel.app/mcp

with the header  Authorization: Bearer <your API key>
(Claude Desktop / Claude Code support per-server headers natively).

Auth: Bearer key in the Authorization header, or ?token=<key> fallback.
Tools operate on the authenticated user's own "BrainBridge Memory" list.
"""

from __future__ import annotations

import contextvars
import os
from urllib.parse import parse_qs

from . import tasks_store
from .tasks_tokens import refresh_and_rotate, resolve_tasks_user

_user_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "bb_mcp_user", default=None
)


def _user() -> dict:
    u = _user_ctx.get()
    if not u:
        raise RuntimeError("unauthorized")
    return u


def _token() -> str:
    return refresh_and_rotate(_user())


def _safe(fn, *a, **kw) -> dict:
    """Run a tasks_store call; never crash the tool — return error dicts."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:300]}


class _AuthMiddleware:
    """Pure ASGI middleware: validate the Bearer key, stash the user."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        token = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            qs = scope.get("query_string", b"").decode()
            token = (parse_qs(qs).get("token") or [""])[0]
        user = resolve_tasks_user(token)
        if not user:
            body = (b'{"error":"unauthorized","hint":"Authorization: Bearer <your key>"}')
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        tok = _user_ctx.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _user_ctx.reset(tok)


def _build() -> "object":
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("BrainBridge Memory", stateless_http=True)

    @mcp.tool()
    def list_notes(limit: int = 100) -> dict:
        """List the user's BrainBridge memory entries (title, content,
        created/updated). Each entry is a note in Google Tasks."""
        try:
            r = _safe(tasks_store.list_notes, _token(), limit=min(max(limit, 1), 300))
            if isinstance(r, dict) and "error" in r:
                return r
            return {"count": len(r), "notes": r}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    @mcp.tool()
    def search_notes(keyword: str, limit: int = 50) -> dict:
        """Search the user's BrainBridge memory by keyword (title + content)."""
        try:
            return _safe(tasks_store.search_notes, _token(), keyword,
                         limit=min(max(limit, 1), 300))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    @mcp.tool()
    def save_note(title: str, content: str) -> dict:
        """Save a memory entry: title + content. Long content is split into
        parts automatically. Returns the created note(s)."""
        try:
            return _safe(tasks_store.create_note, _token(), title, content)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    @mcp.tool()
    def get_note(note_id: str) -> dict:
        """Read one memory entry by id."""
        try:
            n = _safe(tasks_store.get_note, _token(), note_id)
            if n is None:
                return {"error": "no such note"}
            return n
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    @mcp.tool()
    def delete_note(note_id: str) -> dict:
        """Delete a memory entry by id."""
        return {"deleted": bool(_safe(tasks_store.delete_note, _token(), note_id))}

    @mcp.tool()
    def memory_context(limit: int = 8) -> dict:
        """'What is already known' — compact markdown context for a prompt."""
        try:
            notes = _safe(tasks_store.list_notes, _token(), limit=200)
            if isinstance(notes, dict) and "error" in notes:
                return notes
            return {"note_count": len(notes),
                    "context": tasks_store.render_context(notes, limit=limit)}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    @mcp.tool()
    def ask_memory(question: str) -> dict:
        """Ask a question about the user's memory. If the server has
        GEMINI_API_KEY set, a Gemini model answers; otherwise the raw context
        is returned so the calling AI composes the answer."""
        try:
            notes = _safe(tasks_store.list_notes, _token(), limit=200)
            if isinstance(notes, dict) and "error" in notes:
                return notes
            context = tasks_store.render_context(notes, limit=10)
            gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if gemini_key:
                import httpx
                system = ("You are BrainBridge, the user's permanent memory. "
                          "Answer the question using ONLY the memory below; cite "
                          "entry titles when you use them. If it is not in the "
                          "memory, say that honestly.")
                prompt = system + "\n\n--- MEMORY ---\n" + context + \
                    "\n\n--- QUESTION ---\n" + question
                r = httpx.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-2.0-flash:generateContent",
                    params={"key": gemini_key},
                    json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)
                r.raise_for_status()
                cands = r.json().get("candidates", [])
                return {"answer": cands[0]["content"]["parts"][0]["text"]
                        if cands else "(empty)", "model": "gemini-2.0-flash"}
            return {"answer": None, "context": context,
                    "hint": "Set GEMINI_API_KEY on the server for auto answers."}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:300]}

    app = getattr(mcp, "streamable_http_app", None)
    if app is None:  # older SDK
        app = mcp._http_app  # type: ignore[attr-defined]
    return _AuthMiddleware(app())


def http_app():
    """Starlette app to mount under /mcp (used by api/index.py)."""
    return _build()
