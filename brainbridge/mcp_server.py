"""BrainBridge — Model Context Protocol (MCP) server over Google Tasks.

Any MCP-capable AI client (Claude Desktop, Claude Code, Cursor, VS Code,
Cline, ...) can connect with ZERO repo install:

    URL : https://brain-bridge-six.vercel.app/mcp
    Auth: Authorization: Bearer <your BrainBridge API key>

This is a *minimal, stateless* Streamable-HTTP / JSON-RPC implementation
(built on FastAPI, no SDK) so it works on serverless runtimes where long-lived
sessions/lifespans are not available. Only POST is used; responses are plain
JSON (allowed by the MCP spec for stateless servers). The Python MCP client
and Claude Desktop/Cursor-style clients handle this fine.

Tools: list_notes, search_notes, save_note, get_note, delete_note,
memory_context, ask_memory.
"""

from __future__ import annotations

import contextvars
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import tasks_store
from .tasks_tokens import refresh_and_rotate, resolve_tasks_user

PROTOCOL = "2025-11-25"
SERVER_INFO = {"name": "brainbridge", "version": "1.0"}

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


def _safe(fn, *a, **kw):
    """Run a tasks_store call; never crash — return error dicts."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:300]}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _list_notes(limit: int = 100) -> dict:
    r = _safe(tasks_store.list_notes, _token(), limit=min(max(limit, 1), 300))
    if isinstance(r, dict) and "error" in r:
        return r
    return {"count": len(r), "notes": r}


def _search_notes(keyword: str, limit: int = 50) -> dict:
    return _safe(tasks_store.search_notes, _token(), keyword,
                 limit=min(max(limit, 1), 300))


def _save_note(title: str, content: str) -> dict:
    return _safe(tasks_store.create_note, _token(), title, content)


def _get_note(note_id: str) -> dict:
    n = _safe(tasks_store.get_note, _token(), note_id)
    if n is None:
        return {"error": "no such note"}
    return n


def _delete_note(note_id: str) -> dict:
    return {"deleted": bool(_safe(tasks_store.delete_note, _token(), note_id))}


def _memory_context(limit: int = 8) -> dict:
    notes = _safe(tasks_store.list_notes, _token(), limit=200)
    if isinstance(notes, dict) and "error" in notes:
        return notes
    return {"note_count": len(notes),
            "context": tasks_store.render_context(notes, limit=limit)}


def _ask_memory(question: str) -> dict:
    from . import tasks_store as ts
    notes = _safe(ts.list_notes, _token(), limit=200)
    if isinstance(notes, dict) and "error" in notes:
        return notes
    context = ts.render_context(notes, limit=10)
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


TOOLS = [
    {
        "name": "list_notes",
        "description": "List the user's BrainBridge memory entries (title, "
                       "content, created/updated). Each entry is a note in "
                       "Google Tasks.",
        "inputSchema": {"type": "object",
                        "properties": {"limit": {"type": "integer",
                                                 "default": 100}},
                        "additionalProperties": False},
        "fn": _list_notes,
    },
    {
        "name": "search_notes",
        "description": "Search the user's BrainBridge memory by keyword "
                       "(title + content).",
        "inputSchema": {"type": "object",
                        "properties": {"keyword": {"type": "string"},
                                       "limit": {"type": "integer",
                                                 "default": 50}},
                        "required": ["keyword"], "additionalProperties": False},
        "fn": _search_notes,
    },
    {
        "name": "save_note",
        "description": "Save a memory entry: title + content. Long content is "
                       "split into parts automatically.",
        "inputSchema": {"type": "object",
                        "properties": {"title": {"type": "string"},
                                       "content": {"type": "string"}},
                        "required": ["title", "content"],
                        "additionalProperties": False},
        "fn": _save_note,
    },
    {
        "name": "get_note",
        "description": "Read one memory entry by id.",
        "inputSchema": {"type": "object",
                        "properties": {"note_id": {"type": "string"}},
                        "required": ["note_id"], "additionalProperties": False},
        "fn": _get_note,
    },
    {
        "name": "delete_note",
        "description": "Delete a memory entry by id.",
        "inputSchema": {"type": "object",
                        "properties": {"note_id": {"type": "string"}},
                        "required": ["note_id"], "additionalProperties": False},
        "fn": _delete_note,
    },
    {
        "name": "memory_context",
        "description": "'What is already known' — compact markdown context "
                       "ready for an AI prompt.",
        "inputSchema": {"type": "object",
                        "properties": {"limit": {"type": "integer",
                                                 "default": 8}},
                        "additionalProperties": False},
        "fn": _memory_context,
    },
    {
        "name": "ask_memory",
        "description": "Ask a question about the user's memory. If the server "
                       "has GEMINI_API_KEY set, a Gemini model answers; "
                       "otherwise the raw context is returned.",
        "inputSchema": {"type": "object",
                        "properties": {"question": {"type": "string"}},
                        "required": ["question"], "additionalProperties": False},
        "fn": _ask_memory,
    },
]


def _tool_response(result: dict) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False}


app = FastAPI(title="BrainBridge MCP", docs_url=None, redoc_url=None,
              openapi_url=None)


@app.api_route("/", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def mcp_endpoint(request: Request):
    if request.method in ("GET", "DELETE"):
        return JSONResponse({"error": "use POST (stateless JSON-RPC)"},
                            status_code=405)
    # --- auth ---
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    user = resolve_tasks_user(token)
    if not user:
        return JSONResponse(
            {"error": "unauthorized",
             "hint": "Authorization: Bearer <your BrainBridge key>"},
            status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700,
                                       "message": "parse error"}})
    if not isinstance(body, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600,
                                       "message": "invalid request"}})
    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if method == "initialize":
        # claim the same protocol version the client wants, else default
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": params.get("protocolVersion")
                           or PROTOCOL,
                           "capabilities": {"tools": {}},
                           "serverInfo": SERVER_INFO}}
    if method in ("notifications/initialized", "notifications/cancelled",
                  "notifications/roots/list_changed", "$/setLevel",
                  "$/cancelRequest"):
        return JSONResponse(None, status_code=202)
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": [{k: v for k, v in t.items() if k != "fn"}
                                     for t in TOOLS]}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if not tool:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602,
                              "message": f"unknown tool: {name}"}}
        tok = _user_ctx.set(user)
        try:
            result = tool["fn"](**args)
        except TypeError as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602,
                              "message": f"bad arguments: {e}"}}
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e)[:300]}
        finally:
            _user_ctx.reset(tok)
        return {"jsonrpc": "2.0", "id": rid, "result": _tool_response(result)}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}
