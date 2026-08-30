"""BrainBridge — Memory MCP server.

Gives any MCP-capable agent permanent memory through Google NotebookLM
"brains": ask questions, write memory entries, search sources, check health.

Bridges the verified `notebooklm` CLI (notebooklm-py) into the MCP world.
"""

from __future__ import annotations

import json
import subprocess
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

from .auth_flow import auth_check, ensure_auth, logout as _logout

# ---------------------------------------------------------------------------
# Brain registry (verified 2026-08-30, see Abdelkhalik Brain source 6)
# ---------------------------------------------------------------------------
BRAIN_REGISTRY: dict[str, dict[str, str]] = {
    "personal": {
        "id": "41538b98-f0ed-4110-baff-a348d8976563",
        "alias": "abdelkhalik",
        "default_title": "Abdelkhalik  Brain",
        "purpose": "Personal memory: owner profile, skills, resources, session logs.",
    },
    "project": {
        "id": "d3d08d6b-6185-44f8-997e-0c476c478e49",
        "alias": "artisanpro",
        "default_title": "ArtisanPro Brain",
        "purpose": "Product memory: ArtisanPro project context.",
    },
}


def _resolve_brain(target: str | None) -> dict[str, str]:
    """Resolve a brain target (name/alias/id/prefix) to a registry entry."""
    if not target:
        target = "personal"
    t = target.strip().lower()
    for key, entry in BRAIN_REGISTRY.items():
        if t in (key, entry["alias"], entry["default_title"].lower()):
            return entry
        if entry["id"].startswith(t) or entry["id"] == t:
            return entry
        if t and entry["default_title"].lower().startswith(t):
            return entry
    raise ValueError(
        f"Unknown brain '{target}'. Known: "
        + ", ".join(f"{e['alias']} ({e['default_title']})" for e in BRAIN_REGISTRY.values())
    )


def _run(args: list[str], timeout: int = 180) -> str:
    """Run the notebooklm CLI and return combined output."""
    proc = subprocess.run(
        ["notebooklm", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip()


_AUTH_MSG = (
    "No valid NotebookLM session. Run the brain_login tool (opens a Google "
    "sign-in popup, session auto-saves) or brain_login_silent to import the "
    "session from your installed browser."
)


def _require_auth() -> dict | None:
    """Pop the login popup if needed; return an error dict if auth still fails."""
    res = ensure_auth(popup=True)
    if res.get("ok"):
        return None
    return {
        "error": _AUTH_MSG,
        "detail": (res.get("detail") or "")[:600],
        "action": f"run brain_login (action={res.get('action')})",
    }


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "brainbridge",
    instructions=(
        "BrainBridge: permanent memory for AI agents via Google NotebookLM brains. "
        "Use brain_status to verify connectivity, brain_list to discover brains, "
        "brain_ask to query a brain, memory_save to persist knowledge as a dated "
        "memory source (one source per session, never overwrite), memory_sources "
        "to list/search memory entries, memory_read to read one entry."
    ),
)


@mcp.tool()
def brain_status() -> dict:
    """Check if the NotebookLM session is valid (auth + token fetch)."""
    out = _run(["auth", "check", "--test"])
    valid = "Authentication is valid" in out
    return {"valid": valid, "detail": out[:800]}


@mcp.tool()
def brain_login(browser: str | None = None, wait_minutes: int = 6) -> dict:
    """Open a Google sign-in popup; the session is saved automatically.

    Call this when brain_status reports no valid session. A browser tab opens
    where you sign in with your Google account — no cookies to copy/paste.
    Reuses the installed Chrome when you pass `browser='chrome'` (if you're
    already signed in there, the popup auto-authorizes in seconds).

    Args:
        browser: 'chrome' (system Chrome, best UX), 'msedge', or omit for bundled Chromium.
        wait_minutes: How long to wait for sign-in (default 6).
    """
    res = ensure_auth(popup=True, browser=browser)
    return res


@mcp.tool()
def brain_login_silent(browser: str = "chrome") -> dict:
    """No popup: read the Google session from an installed browser instead.

    Use when you are already signed in to Chrome/Firefox/Brave on this machine.

    Args:
        browser: 'chrome' (default), 'firefox', 'brave', 'edge', 'safari', 'arc'.
    """
    from .auth_flow import login_via_browser_cookies
    res = login_via_browser_cookies(browser)
    return res


@mcp.tool()
def brain_logout() -> dict:
    """Forget the saved NotebookLM session (local only — does not sign out of Google)."""
    return _logout()


@mcp.tool()
def brain_keepalive() -> dict:
    """Rotate the session token right now (`notebooklm auth refresh`).

    Run this periodically (or let BrainForge's keepalive.sh loop do it every
    15 min) so the NotebookLM session stays valid for hours/days without
    asking the user to authenticate again. Returns status after the refresh.
    """
    proc = subprocess.run(
        ["notebooklm", "auth", "refresh"],
        capture_output=True, text=True, timeout=120,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok, detail = auth_check(test=True)
    return {"ok": ok, "refresh_exit": proc.returncode, "refresh": out[:300], "detail": detail[:300]}


@mcp.tool()
def brain_list() -> list[dict]:
    """List all available brains (NotebookLM notebooks) with IDs."""
    out = _run(["list", "--json"])
    try:
        data = json.loads(out)
        return [
            {
                "id": n["id"],
                "title": (n.get("title") or "(untitled)").strip(),
                "role": n.get("role"),
                "created_at": n.get("created_at"),
                "modified_at": n.get("modified_at"),
            }
            for n in data.get("notebooks", [])
        ]
    except Exception as e:
        return [{"error": f"could not parse list: {e}", "raw": out[:500]}]


@mcp.tool()
def brain_ask(question: str, brain: str | None = None) -> dict:
    """Ask a question to a brain; the answer cites its sources.

    Args:
        question: The question, in any language.
        brain: Brain key: 'personal'/'project'/'artisanpro'/'abdelkhalik', or a notebook id/prefix.
    """
    blocked = _require_auth()
    if blocked:
        return blocked
    entry = _resolve_brain(brain)
    out = _run(["ask", "--json", "--notebook", entry["id"], question])
    try:
        data = json.loads(out)
        answer = data.get("answer") or data.get("text") or out
        answer = str(answer)
    except Exception:
        answer = out
    return {"brain": entry["default_title"], "answer": answer}


@mcp.tool()
def memory_save(title: str, content: str, brain: str | None = None) -> dict:
    """Persist a memory entry into a brain as a dated Markdown source.

    Args:
        title: Short, descriptive title (no extension needed).
        content: The memory content (Markdown). Include what was verified,
            how it was verified, and why it matters for future sessions.
        brain: Brain key or id (default: personal).
    """
    blocked = _require_auth()
    if blocked:
        return blocked
    entry = _resolve_brain(brain)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{title.strip().lower().replace(' ', '-')}-{today}.md"
    path = Path("/tmp") / filename
    path.write_text(
        f"# {title}\n\n> Memory entry · {today} · via BrainBridge MCP\n\n{content}",
        encoding="utf-8",
    )
    out = _run(
        ["source", "add", str(path), "--type", "file", "--mime-type", "text/markdown",
         "--title", filename.removesuffix(".md"), "--notebook", entry["id"]]
    )
    return {"brain": entry["default_title"], "title": filename, "raw": out}


@mcp.tool()
def memory_sources(keyword: str | None = None, brain: str | None = None) -> list[dict]:
    """List memory sources in a brain, optionally filtered by keyword.

    Args:
        keyword: Case-insensitive substring to filter titles.
        brain: Brain key or id (default: personal).
    """
    blocked = _require_auth()
    if blocked:
        return blocked
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"])
    try:
        data = json.loads(out)
        sources = data.get("sources", data if isinstance(data, list) else [])
    except Exception:
        return [{"error": "could not parse source list", "raw": out[:500]}]
    results = []
    for s in sources:
        title = s.get("title", "")
        if keyword and keyword.lower() not in title.lower():
            continue
        results.append(
            {"id": s.get("id"), "title": title, "kind": s.get("kind"),
             "status": s.get("processing_state") or s.get("status")}
        )
    return results


@mcp.tool()
def memory_read(source_id: str, brain: str | None = None) -> dict:
    """Read the full content of a memory source by id.

    Args:
        source_id: Full source id or unique prefix.
        brain: Brain key or id (default: personal) — used to resolve the notebook.
    """
    blocked = _require_auth()
    if blocked:
        return blocked
    entry = _resolve_brain(brain)
    import tempfile
    tmp = Path(tempfile.mktemp(prefix="brainbridge_read_", suffix=".md"))
    out = _run(
        ["source", "fulltext", source_id, "--notebook", entry["id"],
         "--format", "markdown", "-o", str(tmp)]
    )
    content = tmp.read_text(encoding="utf-8") if tmp.exists() else out
    tmp.unlink(missing_ok=True)
    return {"brain": entry["default_title"], "source_id": source_id, "content": content[:20000]}


@mcp.tool()
def memory_context(brain: str | None = None, max_entries: int = 6) -> dict:
    """Get a compact context digest of a brain: latest memory sources.

    Useful at session start so the agent 'remembers' what was already known.

    Args:
        brain: Brain key or id (default: personal).
        max_entries: How many recent sources to include (default 6).
    """
    blocked = _require_auth()
    if blocked:
        return blocked
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"])
    try:
        data = json.loads(out)
        sources = data.get("sources", data if isinstance(data, list) else [])
    except Exception:
        return {"error": "could not parse source list"}
    sources = sorted(
        sources, key=lambda s: s.get("created_at") or s.get("created") or "", reverse=True
    )[:max_entries]
    return {
        "brain": entry["default_title"],
        "brain_id": entry["id"],
        "recent_sources": [
            {"id": s.get("id"), "title": s.get("title"), "kind": s.get("kind")} for s in sources
        ],
        "tip": "Read a source with memory_read; ask questions with brain_ask.",
    }


if __name__ == "__main__":
    mcp.run()
