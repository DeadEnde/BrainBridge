"""BrainBridge HTTP gateway — give ANY AI model access to your NotebookLM brain.

Exposes the brain over plain HTTP(S) with a bearer key, so any AI agent
(chat assistants, n8n/Zapier, scripts, sandboxes, MCP-less clients) can:

  ask questions            -> brain answers with citations
  save memories            -> dated Markdown source in the notebook
  list / read / context    -> browse the memory archive
  status / refresh         -> session health + token rotation

Every call lands in Google NotebookLM ("the brain"). No MCP client needed.

It also hosts the AUTH endpoints, so "add notebooklm auth" lives here:
  POST /auth/import    <- Cookie-Editor JSON export  (token unlock)
  POST /auth/refresh   <- rotate token (keepalive)

Run:  python3 -m uvicorn brainbridge.gateway:app --host 0.0.0.0 --port 8999
Key:  auto-generated, stored in ~/.notebooklm/gateway_key.txt (chmod 600).
Send it as:  Authorization: Bearer <key>
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Brain registry — keep in sync with server.py (BRAIN_REGISTRY)
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

KEY_FILE = Path.home() / ".notebooklm" / "gateway_key.txt"


def _get_api_key() -> str:
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(24)
    KEY_FILE.write_text(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


API_KEY = _get_api_key()


def _check_key(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing key. Send 'Authorization: Bearer <key>' (file: ~/.notebooklm/gateway_key.txt)")
    if authorization.split(" ", 1)[1].strip() != API_KEY:
        raise HTTPException(403, "Invalid key")


def _run(args: list[str], timeout: int = 180) -> str:
    proc = subprocess.run(
        ["notebooklm", *args], capture_output=True, text=True, timeout=timeout
    )
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _session_ok() -> tuple[bool, str]:
    out = _run(["auth", "check", "--test"], timeout=120)
    return ("Authentication is valid" in out), out[:600]


def _resolve_brain(target: str | None) -> dict[str, str]:
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
    raise HTTPException(404, f"Unknown brain '{target}'. Known: " + ", ".join(BRAIN_REGISTRY))


def _require_session() -> None:
    ok, detail = _session_ok()
    if ok:
        return
    raise HTTPException(
        401,
        "NotebookLM session expired. Unlock it: POST /auth/import with a fresh "
        "Cookie-Editor JSON export (notebooklm.google.com + accounts.google.com), "
        "then POST /auth/refresh. Detail: " + detail[:300],
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class AskIn(BaseModel):
    question: str
    brain: str | None = None


class SaveIn(BaseModel):
    title: str
    content: str
    brain: str | None = None


class ReadIn(BaseModel):
    source_id: str
    brain: str | None = None


class ImportIn(BaseModel):
    cookies: list[dict] | None = None  # Cookie-Editor export
    file: str | None = None            # or path to a JSON file on disk


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="BrainBridge Gateway", version="1.0",
              description="NotebookLM brain bridge — any AI can ask & save.")


@app.get("/")
def home():
    return {
        "name": "BrainBridge Gateway",
        "status": "ok",
        "auth": "Authorization: Bearer <key>  (file ~/.notebooklm/gateway_key.txt)",
        "endpoints": {
            "GET  /health": "public",
            "GET  /status": "session health",
            "GET  /brains": "your notebooks",
            "POST /ask": {"question": "...", "brain": "personal|project"},
            "POST /memory/save": {"title": "...", "content": "..."},
            "GET  /memory/list": "?keyword=&brain=",
            "GET  /memory/read": "?source_id=&brain=",
            "GET  /memory/context": "?brain=",
            "POST /auth/import": "cookie JSON (unlock session)",
            "POST /auth/refresh": "rotate token (keepalive)",
        },
        "ai_prompt": (
            "You have access to the user's permanent brain (Google NotebookLM) via "
            "BrainBridge: POST {base}/ask to answer questions from memory, "
            "POST {base}/memory/save to persist anything important as a dated source, "
            "GET {base}/memory/context to load 'what is already known'. "
            "Always save session conclusions; never overwrite — one source per session."
        ),
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "brainbridge-gateway"}


@app.get("/status")
def status(authorization: str | None = Header(default=None)):
    _check_key(authorization)
    valid, detail = _session_ok()
    return {"valid": valid, "detail": detail[:600]}


@app.get("/brains")
def brains(authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    out = _run(["list", "--json"])
    try:
        data = json.loads(out)
        return {"notebooks": [{"id": n.get("id"), "title": n.get("title"),
                               "role": n.get("role")} for n in data.get("notebooks", [])]}
    except Exception:
        return {"error": "could not parse", "raw": out[:400]}


@app.post("/ask")
def ask(req: AskIn, authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    entry = _resolve_brain(req.brain)
    out = _run(["ask", "--json", "--notebook", entry["id"], req.question], timeout=300)
    try:
        data = json.loads(out)
        answer = data.get("answer") or data.get("text") or out
    except Exception:
        answer = out
    return {"brain": entry["default_title"], "answer": str(answer)}


@app.post("/memory/save")
def memory_save(req: SaveIn, authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    entry = _resolve_brain(req.brain)
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    filename = f"{req.title.strip().lower().replace(' ', '-')}-{today}.md"
    md = f"# {req.title}\n\n> Memory entry · {today} · via BrainBridge\n\n{req.content}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(md)
        tmp = f.name
    out = _run(["source", "add", tmp, "--type", "file", "--mime-type", "text/markdown",
                "--title", filename.removesuffix(".md"), "--notebook", entry["id"]])
    Path(tmp).unlink(missing_ok=True)
    return {"brain": entry["default_title"], "title": filename, "raw": out[:400]}


@app.get("/memory/list")
def memory_list(keyword: str | None = None, brain: str | None = None,
                authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"])
    try:
        data = json.loads(out)
        sources = data.get("sources", data if isinstance(data, list) else [])
    except Exception:
        return {"error": "could not parse", "raw": out[:400]}
    res = []
    for s in sources:
        title = s.get("title", "")
        if keyword and keyword.lower() not in title.lower():
            continue
        res.append({"id": s.get("id"), "title": title, "kind": s.get("kind"),
                    "status": s.get("processing_state") or s.get("status")})
    return {"brain": entry["default_title"], "sources": res}


@app.get("/memory/read")
def memory_read(source_id: str, brain: str | None = None,
                authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    entry = _resolve_brain(brain)
    tmp = Path(tempfile.mktemp(prefix="gateway_read_", suffix=".md"))
    out = _run(["source", "fulltext", source_id, "--notebook", entry["id"],
                "--format", "markdown", "-o", str(tmp)])
    content = tmp.read_text(encoding="utf-8") if tmp.exists() else out
    tmp.unlink(missing_ok=True)
    return {"brain": entry["default_title"], "source_id": source_id,
            "content": content[:20000]}


@app.get("/memory/context")
def memory_context(brain: str | None = None, max_entries: int = 6,
                   authorization: str | None = Header(default=None)):
    _check_key(authorization)
    _require_session()
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"])
    try:
        data = json.loads(out)
        sources = data.get("sources", data if isinstance(data, list) else [])
    except Exception:
        return {"error": "could not parse", "raw": out[:400]}
    sources = sorted(sources, key=lambda s: s.get("created_at") or s.get("created") or "", reverse=True)[:max_entries]
    return {"brain": entry["default_title"], "brain_id": entry["id"],
            "recent_sources": [{"id": s.get("id"), "title": s.get("title")} for s in sources]}


@app.post("/auth/import")
def auth_import(req: ImportIn, authorization: str | None = Header(default=None)):
    """Unlock: paste a Cookie-Editor JSON export (notebooklm.google.com + accounts.google.com)."""
    _check_key(authorization)
    if not req.cookies and not req.file:
        raise HTTPException(400, "Provide 'cookies' (array) or 'file' (path to JSON)")
    if req.file:
        data = json.loads(Path(req.file).read_text())
    else:
        data = req.cookies
    # normalize to Cookie-Editor export shape: [{"name","value","domain",...}]
    cookies = data if isinstance(data, list) else data.get("cookies", data)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cookies, f)
        tmp = f.name
    out = _run(["auth", "import-cookies", tmp], timeout=180)
    Path(tmp).unlink(missing_ok=True)
    if not _session_ok()[0]:
        raise HTTPException(401, "Import did not produce a valid session. " + out[:400])
    return {"ok": True, "session": "valid", "detail": out[:300]}


@app.post("/auth/refresh")
def auth_refresh(authorization: str | None = Header(default=None)):
    """Rotate the token now (keepalive). Use /status to confirm."""
    _check_key(authorization)
    out = _run(["auth", "refresh"], timeout=120)
    valid, detail = _session_ok()
    return {"ok": valid, "refresh": out[:300], "detail": detail[:300]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BRAINBRIDGE_GATEWAY_PORT", "8999")))
