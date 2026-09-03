"""BrainBridge HTTP gateway — give ANY AI model access to your NotebookLM brain.

Single-owner mode (classic):
  Ask / save / read via a bearer key, one Google session (the owner's).
  POST /auth/import, /auth/refresh, GET /auth/export keep that session alive.

Multi-user mode (BrainBridge Multi):
  POST /auth/register    <- paste a Cookie-Editor export (or storage state)
  POST /auth/tickets     <- managed login: create a ticket (hosted browser)
  GET  /auth/tickets/{id}<- poll a ticket; when done it returns your API key
  Each user gets a private API key + a private NotebookLM session (per-user
  storage_state). /ask, /memory/* etc. resolve the key -> the user's session.

Storage of users: auto-detected (Upstash KV -> Vercel Blob -> local files),
see brainbridge/users_store.py.

Every call lands in Google NotebookLM ("the brain"). No MCP client needed.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import httpx
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .users_store import get_store, store_name
from .secret import decrypt_state, encrypt_state, secrets_enabled
from . import oauth_flow
from . import tasks_store

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

# ---------------------------------------------------------------------------
# Key + session storage (local file OR Vercel env vars)
# ---------------------------------------------------------------------------
KEY_FILE = Path.home() / ".notebooklm" / "gateway_key.txt"


def _get_api_key() -> str:
    env = os.environ.get("BRAINBRIDGE_GATEWAY_KEY", "").strip()
    if env:
        return env
    try:
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
    except OSError:
        return ""


API_KEY = _get_api_key()

# Session state: local default, or overridden by BRAINBRIDGE_STORAGE (Vercel /tmp)
DEFAULT_STORAGE = str(Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json")
STORAGE = Path(os.environ.get("BRAINBRIDGE_STORAGE", DEFAULT_STORAGE)).expanduser()
OWNER_SESSION = os.environ.get("BRAINBRIDGE_OWNER_SESSION", "owner")


def bootstrap_state() -> None:
    """On boot (Vercel): restore the owner session from BRAINBRIDGE_STATE_B64.

    Gzipped payloads (gzip magic 1f 8b) are accepted so the value fits the
    ~4KB Vercel env-var budget. Always re-restored on boot: /tmp is ephemeral.
    """
    state_b64 = os.environ.get("BRAINBRIDGE_STATE_B64", "").strip()
    if not state_b64:
        return
    STORAGE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = base64.b64decode(state_b64)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        if len(data) > 50:
            STORAGE.write_bytes(data)
        else:
            print("[bridge] BRAINBRIDGE_STATE_B64 decoded to <50 bytes; ignoring", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] could not restore session state: {e}", file=sys.stderr)


def _state_b64() -> str:
    if STORAGE.exists():
        return base64.b64encode(STORAGE.read_bytes()).decode()
    return ""


# ---------------------------------------------------------------------------
# Auth: owner key OR per-user key -> context dict
# ---------------------------------------------------------------------------
def _require_api_key() -> None:
    if not API_KEY:
        raise HTTPException(
            503,
            "BRAINBRIDGE_GATEWAY_KEY is not set on the server.",
        )


def _authorize(authorization: str | None) -> dict:
    """Return {'user': None} for the owner, or {'user': rec} for a user key."""
    _require_api_key()
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token == API_KEY:
            return {"user": None}
        store = get_store()
        rec = store.get_user(token)
        if not rec:
            # Keys are base64url and may end with '-'/'_' which people often
            # drop when copying: accept the variants too.
            for cand in (token + "-", token + "_", token[:-1] if token.endswith(("-", "_")) else token):
                if cand != token and cand:
                    rec = store.get_user(cand)
                    if rec:
                        break
        if rec:
            return {"user": rec}
        raise HTTPException(403, "Invalid key")
    raise HTTPException(
        401,
        "Missing key. Send 'Authorization: Bearer <key>' "
        "(owner key = env BRAINBRIDGE_GATEWAY_KEY; user keys come from /auth/register).",
    )


_user_tmp: dict[str, str] = {}


def _storage_for(ctx: dict) -> Path:
    if ctx.get("user") is None:
        return STORAGE
    key: str = ctx["user"]["key"]
    path = _user_tmp.get(key)
    if not path:
        path = str(Path(tempfile.gettempdir()) / f"bbstate_{key[:12]}.json")
        _user_tmp[key] = path
    p = Path(path)
    # refresh the user's state file on demand (cheap compare-by-size+check)
    state_b64 = ctx["user"].get("state_b64", "")
    try:
        new = decrypt_state(state_b64)
        if not p.exists() or p.stat().st_size != len(new):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(new)
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] user {key[:8]}: bad state_b64: {e}", file=sys.stderr)
    return p


def _run(args: list[str], storage: Path | None = None, timeout: int = 180) -> str:
    """Run the notebooklm CLI (works locally AND on Vercel: python -m notebooklm)."""
    s = str(storage) if storage is not None else str(STORAGE)
    proc = subprocess.run(
        [sys.executable, "-m", "notebooklm", "--storage", s, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _session_ok(ctx: dict) -> tuple[bool, str]:
    out = _run(["auth", "check", "--test"], storage=_storage_for(ctx), timeout=120)
    return ("Authentication is valid" in out), out[:600]


def _require_session(ctx: dict) -> None:
    """Session must be valid; if not, try ONE automatic token rotation
    (auth refresh asks Google for fresh `__Secure-1PSIDTS`) before failing."""
    ok, detail = _session_ok(ctx)
    if ok:
        return
    try:
        _run(["auth", "refresh"], storage=_storage_for(ctx), timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] auto-refresh failed: {e}", file=sys.stderr)
    ok2, detail2 = _session_ok(ctx)
    if ok2:
        return
    raise HTTPException(
        401,
        "NotebookLM session expired and auto-refresh did not recover it. "
        "Re-authenticate: POST /auth/register with a fresh Cookie-Editor export, "
        "or via managed login (POST /auth/tickets). Detail: " + detail2[:300],
    )


_ensure_session = _require_session  # alias (same auto-refresh behaviour)


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


class RegisterIn(BaseModel):
    cookies: list[dict] | None = None  # Cookie-Editor JSON export (array or {cookies:[...]})
    state: dict | None = None          # full Playwright storage_state object
    state_b64: str | None = None       # base64 (raw or gzipped) of storage_state.json
    label: str | None = None           # optional display name


class TicketUrlIn(BaseModel):
    browser_url: str
    browser_password: str | None = None


class CollectIn(BaseModel):
    state_b64: str
    label: str | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="BrainBridge Gateway", version="2.0",
              description="NotebookLM brain bridge — any AI can ask & save. "
                          "Multi-user: each API key = its own private NotebookLM session.")


@app.get("/")
def home():
    return {
        "name": "BrainBridge Gateway",
        "version": "2.0 (multi-user)",
        "status": "ok",
        "store": store_name(),
        "auth": "Authorization: Bearer <key> (owner key, or your /auth/register key)",
        "endpoints": {
            "GET  /health": "public",
            "GET  /status": "session health (for YOUR key)",
            "GET  /brains": "your notebooks",
            "POST /ask": {"question": "...", "brain": "personal|project"},
            "POST /memory/save": {"title": "...", "content": "..."},
            "GET  /memory/list": "?keyword=&brain=",
            "GET  /memory/read": "?source_id=&brain=",
            "GET  /memory/context": "?brain=",
            "POST /auth/register": "MULTI-USER: paste cookies/state -> get your API key",
            "POST /auth/tickets": "MULTI-USER: create a managed-login ticket",
            "GET  /auth/tickets/{id}": "poll ticket -> your API key",
            "GET  /users": "owner only: list users",
            "DELETE /users/{key}": "owner only: remove a user",
            "POST /auth/import": "owner only: cookie JSON (unlock session)",
            "POST /auth/refresh": "rotate token (keepalive; works for any key)",
            "GET  /auth/export": "owner only: base64 session state",
        },
        "connect_page": "/connect",
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


@app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
def connect_page():
    """The multi-user connect page (paste-export + managed login tabs)."""
    p = Path(__file__).resolve().parent.parent / "api" / "connect.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8").replace("{{API_BASE}}", ""))
    return HTMLResponse("<h1>BrainBridge Connect</h1><p>POST /auth/register</p>")


@app.get("/status")
def status(authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    valid, detail = _session_ok(ctx)
    who = "owner" if ctx.get("user") is None else f"user·{ctx['user']['key'][:8]}"
    return {"valid": valid, "who": who, "detail": detail[:600]}


@app.get("/brains")
def brains(authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    _require_session(ctx)
    out = _run(["list", "--json"], storage=_storage_for(ctx))
    try:
        data = json.loads(out)
        return {"notebooks": [{"id": n.get("id"), "title": n.get("title"),
                               "role": n.get("role")} for n in data.get("notebooks", [])]}
    except Exception:
        return {"error": "could not parse", "raw": out[:400]}


@app.post("/ask")
def ask(req: AskIn, authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    _require_session(ctx)
    entry = _resolve_brain(req.brain)
    out = _run(["ask", "--json", "--notebook", entry["id"], req.question],
               storage=_storage_for(ctx), timeout=300)
    try:
        data = json.loads(out)
        answer = data.get("answer") or data.get("text") or out
    except Exception:
        answer = out
    return {"brain": entry["default_title"], "answer": str(answer)}


@app.post("/memory/save")
def memory_save(req: SaveIn, authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    _require_session(ctx)
    entry = _resolve_brain(req.brain)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{req.title.strip().lower().replace(' ', '-')}-{today}.md"
    md = f"# {req.title}\n\n> Memory entry · {today} · via BrainBridge\n\n{req.content}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(md)
        tmp = f.name
    out = _run(["source", "add", tmp, "--type", "file", "--mime-type", "text/markdown",
                "--title", filename.removesuffix(".md"), "--notebook", entry["id"]],
               storage=_storage_for(ctx))
    Path(tmp).unlink(missing_ok=True)
    return {"brain": entry["default_title"], "title": filename, "raw": out[:400]}


@app.get("/memory/list")
def memory_list(keyword: str | None = None, brain: str | None = None,
                authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    _require_session(ctx)
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"], storage=_storage_for(ctx))
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
    ctx = _authorize(authorization)
    _require_session(ctx)
    entry = _resolve_brain(brain)
    tmp = Path(tempfile.mktemp(prefix="gateway_read_", suffix=".md"))
    out = _run(["source", "fulltext", source_id, "--notebook", entry["id"],
                "--format", "markdown", "-o", str(tmp)], storage=_storage_for(ctx))
    content = tmp.read_text(encoding="utf-8") if tmp.exists() else out
    tmp.unlink(missing_ok=True)
    return {"brain": entry["default_title"], "source_id": source_id,
            "content": content[:20000]}


@app.get("/memory/context")
def memory_context(brain: str | None = None, max_entries: int = 6,
                   authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    _require_session(ctx)
    entry = _resolve_brain(brain)
    out = _run(["source", "list", "--notebook", entry["id"], "--json"], storage=_storage_for(ctx))
    try:
        data = json.loads(out)
        sources = data.get("sources", data if isinstance(data, list) else [])
    except Exception:
        return {"error": "could not parse", "raw": out[:400]}
    sources = sorted(sources, key=lambda s: s.get("created_at") or s.get("created") or "", reverse=True)[:max_entries]
    return {"brain": entry["default_title"], "brain_id": entry["id"],
            "recent_sources": [{"id": s.get("id"), "title": s.get("title")} for s in sources]}


# ---------------------------------------------------------------------------
# MULTI-USER: register + per-user auth
# ---------------------------------------------------------------------------
def _decode_state(state_b64: str) -> bytes:
    data = base64.b64decode(state_b64)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _validate_state(storage: Path) -> tuple[bool, str]:
    out = _run(["auth", "check", "--test"], storage=storage, timeout=120)
    return ("Authentication is valid" in out), out[:300]


@app.post("/auth/register")
def auth_register(req: RegisterIn):
    """Public: paste a Cookie-Editor export (or storage state) -> get YOUR API key.

    The gateway validates the session with Google before handing out the key.
    """
    store = get_store()
    if req.state_b64:
        try:
            data = _decode_state(req.state_b64)
        except Exception as e:
            raise HTTPException(400, f"state_b64 invalid: {e}")
    elif req.state:
        data = json.dumps(req.state).encode()
    elif req.cookies:
        cookies = req.cookies if isinstance(req.cookies, list) else req.cookies.get("cookies", req.cookies)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cookies, f)
            tmp_cookies = f.name
        storage = Path(tempfile.mktemp(prefix="bb_reg_", suffix=".json"))
        storage.parent.mkdir(parents=True, exist_ok=True)
        out = _run(["auth", "import-cookies", tmp_cookies], storage=storage, timeout=180)
        Path(tmp_cookies).unlink(missing_ok=True)
        # If the export lacks __Secure-1PSIDTS (Cookie-Editor can't read HttpOnly
        # after Chrome's App-Bound Encryption), the browser worker can MINT it
        # from SID: put the cookies in the pending queue and return 202.
        if "Missing required cookies" in out:
            pid = secrets.token_hex(8)
            store.put_pending({
                "pending_id": pid,
                "cookies": cookies,
                "label": (req.label or "").strip()[:80] or "user",
                "status": "wait",
                "created": int(datetime.now(timezone.utc).timestamp()),
            })
            return {
                "ok": True,
                "pending_id": pid,
                "status": "mint_required",
                "tip": ("Your export lacks __Secure-1PSIDTS (Google rotates it per "
                        "browser). A browser worker will mint fresh tokens from your "
                        "SID automatically — poll GET /auth/pending/{pid} for the key."),
            }
        valid, detail = _validate_state(storage)
        if not valid:
            storage.unlink(missing_ok=True)
            raise HTTPException(401, "Import did not produce a valid session. " + (out + " " + detail)[:400])
        data = storage.read_bytes()
        storage.unlink(missing_ok=True)
    else:
        raise HTTPException(400, "Provide 'cookies', 'state' or 'state_b64'")

    # validate before storing
    storage = Path(tempfile.mktemp(prefix="bb_reg_", suffix=".json"))
    storage.write_bytes(data)
    valid, detail = _validate_state(storage)
    if not valid:
        storage.unlink(missing_ok=True)
        raise HTTPException(401, "Session is not valid for Google. Detail: " + detail[:300])

    key = secrets.token_urlsafe(24)
    rec = {
        "key": key,
        "label": (req.label or "").strip()[:80] or "user",
        "state_b64": encrypt_state(data),
        "created": int(datetime.now(timezone.utc).timestamp()),
        "updated": int(datetime.now(timezone.utc).timestamp()),
        "note": "register",
    }
    store.put_user(rec)
    return {
        "ok": True,
        "api_key": key,
        "label": rec["label"],
        "base": str(os.environ.get("BRAINBRIDGE_PUBLIC_BASE", "")).rstrip("/") or "/",
        "usage": {
            "status": "GET /status",
            "brains": "GET /brains",
            "ask": "POST /ask  {question, brain}",
            "save": "POST /memory/save  {title, content, brain}",
            "context": "GET /memory/context",
        },
        "tip": "Send 'Authorization: Bearer <api_key>' with every call. Your session "
               "is private to this key and refreshed automatically.",
    }


@app.post("/auth/refresh")
def auth_refresh(authorization: str | None = Header(default=None)):
    """Rotate the token NOW (keepalive) for whichever key you send."""
    ctx = _authorize(authorization)
    storage = _storage_for(ctx)
    out = _run(["auth", "refresh"], storage=storage, timeout=120)
    valid, detail = _validate_state(storage)
    if ctx.get("user") is not None:
        rec = dict(ctx["user"])
        if valid and storage.exists():
            rec["state_b64"] = base64.b64encode(storage.read_bytes()).decode()
        rec["updated"] = int(datetime.now(timezone.utc).timestamp())
        get_store().put_user(rec)
    return {"ok": valid, "refresh": out[:300], "detail": detail[:300],
            "state_b64": _state_b64() if valid and ctx.get("user") is None else ""}


@app.post("/auth/import")
def auth_import(req: ImportIn, authorization: str | None = Header(default=None)):
    """Owner only: unlock the owner session (paste a Cookie-Editor JSON export)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only; users refresh their own key via /auth/refresh")
    if not req.cookies and not req.file:
        raise HTTPException(400, "Provide 'cookies' (array) or 'file' (path to JSON)")
    if req.file:
        data = json.loads(Path(req.file).read_text())
    else:
        data = req.cookies
    cookies = data if isinstance(data, list) else data.get("cookies", data)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cookies, f)
        tmp = f.name
    out = _run(["auth", "import-cookies", tmp], timeout=180)
    Path(tmp).unlink(missing_ok=True)
    valid, detail = _session_ok(ctx)
    if not valid:
        raise HTTPException(401, "Import did not produce a valid session. " + out[:400])
    return {"ok": True, "session": "valid", "detail": (out + "\n" + detail)[:400],
            "state_b64": _state_b64()}


@app.get("/auth/export")
def auth_export(authorization: str | None = Header(default=None)):
    """Owner only: base64 of the current session state (for BRAINBRIDGE_STATE_B64)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    state = _state_b64()
    return {"ok": bool(state), "state_b64": state,
            "tip": "Paste this into the Vercel env var BRAINBRIDGE_STATE_B64."}


# ---------------------------------------------------------------------------
# MULTI-USER: managed-login tickets (hosted browser flow)
# ---------------------------------------------------------------------------
@app.post("/auth/tickets")
def ticket_create(label: str | None = None):
    """Public: create a managed-login ticket. Poll GET /auth/tickets/{id}."""
    store = get_store()
    tid = secrets.token_hex(8)
    rec = {"ticket_id": tid, "status": "pending", "label": (label or "").strip()[:80],
           "created": int(datetime.now(timezone.utc).timestamp()),
           "browser_url": os.environ.get("BRAINBRIDGE_BROWSER_URL", ""),
           "browser_password": os.environ.get("BRAINBRIDGE_BROWSER_PASSWORD", "")}
    store.put_ticket(rec)
    return {"ok": True, "ticket_id": tid, "status": rec["status"],
            "browser_url": rec["browser_url"], "poll": f"/auth/tickets/{tid}"}


@app.get("/auth/tickets/next")
def ticket_next(authorization: str | None = Header(default=None)):
    """Owner only: pick the oldest pending ticket (the hosted-browser worker)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    now = int(datetime.now(timezone.utc).timestamp())
    pending = [t for t in store.list_tickets()
               if t.get("status") == "pending"
               or (t.get("status") == "open" and now - int(t.get("claimed", 0)) > 1800)]
    pending.sort(key=lambda t: t.get("created", 0))
    if not pending:
        return {"ticket_id": None}
    t = pending[0]
    t["status"] = "open"  # claimed by the worker (workers that died get reclaimed)
    t["claimed"] = now
    store.put_ticket(t)
    return {"ticket_id": t["ticket_id"], "status": "open", "label": t.get("label", "")}


@app.post("/auth/tickets/{ticket_id}/url")
def ticket_set_url(ticket_id: str, req: TicketUrlIn, authorization: str | None = Header(default=None)):
    """Owner only: the worker tells the gateway the hosted-browser URL + password."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    t = store.get_ticket(ticket_id)
    if not t:
        raise HTTPException(404, "no such ticket")
    t["browser_url"] = req.browser_url
    pw = req.browser_password or os.environ.get("BRAINBRIDGE_BROWSER_PASSWORD", "")
    if pw:
        t["browser_password"] = pw
    store.put_ticket(t)
    return {"ok": True}


@app.post("/auth/tickets/{ticket_id}/collect")
def ticket_collect(ticket_id: str, req: CollectIn, authorization: str | None = Header(default=None)):
    """Owner only: worker uploads the session state captured in the hosted browser."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    t = store.get_ticket(ticket_id)
    if not t:
        raise HTTPException(404, "no such ticket")
    try:
        data = _decode_state(req.state_b64)
    except Exception as e:
        raise HTTPException(400, f"state_b64 invalid: {e}")
    storage = Path(tempfile.mktemp(prefix="bb_collect_", suffix=".json"))
    storage.write_bytes(data)
    valid, detail = _validate_state(storage)
    if not valid:
        storage.unlink(missing_ok=True)
        raise HTTPException(401, "Captured session is not valid. Detail: " + detail[:300])
    key = secrets.token_urlsafe(24)
    rec = {
        "key": key,
        "label": (req.label or t.get("label") or "").strip()[:80] or "user",
        "state_b64": encrypt_state(data),
        "created": int(datetime.now(timezone.utc).timestamp()),
        "updated": int(datetime.now(timezone.utc).timestamp()),
        "note": f"ticket:{ticket_id}",
    }
    store.put_user(rec)
    t["status"] = "done"
    t["api_key"] = key
    t["done"] = int(datetime.now(timezone.utc).timestamp())
    store.put_ticket(t)
    storage.unlink(missing_ok=True)
    return {"ok": True, "status": "done", "api_key": key}


@app.post("/auth/tickets/{ticket_id}/fail")
def ticket_fail(ticket_id: str, authorization: str | None = Header(default=None)):
    """Owner only: mark a ticket failed (login timeout / browser error)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    t = store.get_ticket(ticket_id)
    if not t:
        raise HTTPException(404, "no such ticket")
    t["status"] = "failed"
    store.put_ticket(t)
    return {"ok": True}


@app.get("/auth/tickets/{ticket_id}")
def ticket_get(ticket_id: str):
    """Public: poll your ticket. When status=done, 'api_key' is YOUR key."""
    t = get_store().get_ticket(ticket_id)
    if not t:
        raise HTTPException(404, "no such ticket")
    return {
        "ticket_id": ticket_id,
        "status": t.get("status"),
        "browser_url": t.get("browser_url", ""),
        "browser_password": t.get("browser_password", ""),
        "api_key": t.get("api_key") if t.get("status") == "done" else None,
    }


# ---------------------------------------------------------------------------
# MULTI-USER: cookie-mint queue
@app.get("/auth/pending/next")
def pending_next(authorization: str | None = Header(default=None)):
    """Owner/worker: pick the oldest waiting mint request."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    waiting = [r for r in store.list_pending()
               if r.get("pending_id") != "__hb__" and r.get("status") in ("wait", "minting")]
    waiting.sort(key=lambda r: r.get("created", 0))
    if not waiting:
        return {"pending_id": None}
    r = waiting[0]
    r["status"] = "minting"
    store.put_pending(r)
    return {"pending_id": r["pending_id"], "label": r.get("label", ""),
            "cookies": r.get("cookies", [])}

@app.get("/auth/pending/{pending_id}")
def pending_get(pending_id: str):
    """Public: poll your mint request. When status=done, 'api_key' is YOUR key."""
    rec = get_store().get_pending(pending_id)
    if not rec:
        raise HTTPException(404, "no such pending request")
    return {"pending_id": pending_id, "status": rec.get("status"),
            "api_key": rec.get("api_key") if rec.get("status") == "done" else None}
# ---------------------------------------------------------------------------




@app.post("/auth/pending/{pending_id}/mint")
def pending_mint(pending_id: str, req: CollectIn, authorization: str | None = Header(default=None)):
    """Owner/worker: upload the state AFTER a browser minted fresh tokens from SID."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    r = store.get_pending(pending_id)
    if not r:
        raise HTTPException(404, "no such pending request")
    try:
        data = _decode_state(req.state_b64)
    except Exception as e:
        raise HTTPException(400, f"state_b64 invalid: {e}")
    storage = Path(tempfile.mktemp(prefix="bb_mint_", suffix=".json"))
    storage.write_bytes(data)
    valid, detail = _validate_state(storage)
    if not valid:
        storage.unlink(missing_ok=True)
        raise HTTPException(401, "Minted session is not valid. Detail: " + detail[:300])
    key = secrets.token_urlsafe(24)
    store.put_user({
        "key": key,
        "label": (req.label or r.get("label") or "").strip()[:80] or "user",
        "state_b64": encrypt_state(data),
        "created": int(datetime.now(timezone.utc).timestamp()),
        "updated": int(datetime.now(timezone.utc).timestamp()),
        "note": "minted",
    })
    r["status"] = "done"
    r["api_key"] = key
    r["done"] = int(datetime.now(timezone.utc).timestamp())
    store.put_pending(r)
    storage.unlink(missing_ok=True)
    return {"ok": True, "status": "done", "api_key": key}


@app.post("/auth/pending/{pending_id}/failed")
def pending_failed(pending_id: str, authorization: str | None = Header(default=None)):
    """Owner/worker: Google rejected the mint (SID-only no longer enough)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    r = store.get_pending(pending_id)
    if not r:
        raise HTTPException(404, "no such pending request")
    r["status"] = "failed"
    r["failed"] = int(datetime.now(timezone.utc).timestamp())
    store.put_pending(r)
    return {"ok": True, "status": "failed"}


# ---------------------------------------------------------------------------
# MULTI-USER: batch token refresh (guardian / cron)
# ---------------------------------------------------------------------------
def _refresh_user(u: dict) -> bool:
    """Refresh one user's session state on disk with Google (rotates 1PSIDTS)."""
    store = get_store()
    try:
        storage = Path(tempfile.mktemp(prefix="bb_ref_", suffix=".json"))
        storage.write_bytes(decrypt_state(u.get("state_b64", "")))
        _run(["auth", "refresh"], storage=storage, timeout=120)
        valid, _ = _validate_state(storage)
        if valid:
            u["state_b64"] = encrypt_state(storage.read_bytes())
            u["updated"] = int(datetime.now(timezone.utc).timestamp())
            store.put_user(u)
            storage.unlink(missing_ok=True)
            return True
        storage.unlink(missing_ok=True)
        return False
    except Exception:  # noqa: BLE001
        return False


@app.post("/auth/refresh-all")
def refresh_all(limit: int = 20, offset: int = 0,
                authorization: str | None = Header(default=None)):
    """Owner: refresh user sessions in batches (guardian calls this every X min)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    users = sorted(store.list_users(), key=lambda u: u.get("created", 0))
    total = len(users)
    window = users[offset:offset + max(1, min(limit, 50))]
    ok = 0
    for u in window:
        if _refresh_user(u):
            ok += 1
    next_offset = offset + len(window) if offset + len(window) < total else None
    return {"ok": ok, "fail": len(window) - ok, "total": total,
            "scanned": len(window), "next_offset": next_offset}


# ---------------------------------------------------------------------------
# BrainBridge Notes — official Google OAuth (Tasks) flow
# ---------------------------------------------------------------------------
def _refresh_and_rotate(u: dict) -> str:
    """Refresh Google's access token for a tasks user and PERSIST the rotated
    refresh token Google returns (Google invalidates the previous one right
    away for unverified apps). Keeps the previous token as fallback."""
    cands: list[tuple[str, str]] = []
    try:
        cands.append(("current", decrypt_state(u["refresh_token"]).decode()))
    except Exception:  # noqa: BLE001
        pass
    if u.get("refresh_token_prev"):
        try:
            cands.append(("prev", decrypt_state(u["refresh_token_prev"]).decode()))
        except Exception:  # noqa: BLE001
            pass
    last: Exception | None = None
    for slot, tok in cands:
        try:
            at, new_rt = oauth_flow.refresh_access_token(tok)
        except HTTPException as e:
            last = e
            continue
        if new_rt:
            if slot == "current":
                u["refresh_token_prev"] = u["refresh_token"]
            u["refresh_token"] = encrypt_state(new_rt.encode())
            get_store().put_user(u)
        return at
    raise HTTPException(
        401, f"Refresh token rejected (last error: {last})" if last
        else "No refresh token stored for this user"
    )


def _tasks_token(ctx: dict) -> str:
    """For a tasks-provider user: (re)issue an access token from the refresh token."""
    u = ctx.get("user")
    if not u or u.get("provider") != "tasks":
        raise HTTPException(400, "This key is not a Notes (Google Tasks) user.")
    return _refresh_and_rotate(u)


def _gcalls(fn, *a, **kw):
    """Run a tasks_store call and convert Google's errors into readable 502s."""
    try:
        return fn(*a, **kw)
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:250]
        raise HTTPException(
            502,
            f"Google Tasks API {e.response.status_code}: {body}",
        ) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Google Tasks error: {str(e)[:250]}") from e


def _sign_secret() -> bytes:
    s = os.environ.get("BRAINBRIDGE_SECRET", "").strip().encode()
    return s or b"bb-dev-fallback-secret"


def _pack_redirect(redirect: str | None) -> str | None:
    """Embed a client callback URL inside the OAuth `state` (HMAC-signed),
    so the consent flow can hand the API key back to the calling app."""
    if not redirect:
        return None
    r = redirect.strip()[:500]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", r):
        return None  # not a URL (blocks javascript:, data: only via scheme check)
    if r.lower().startswith(("javascript:", "data:", "file:", "vbscript:")):
        return None
    blob = base64.urlsafe_b64encode(
        json.dumps({"r": r, "n": secrets.token_urlsafe(8)}).encode()
    ).decode()
    sig = hmac.new(_sign_secret(), blob.encode(), hashlib.sha256).hexdigest()[:22]
    return f"{blob}.{sig}"


def _unpack_redirect(state: str | None) -> str | None:
    """Reverse of _pack_redirect. Returns None on any tamper/parse issue."""
    if not state or "." not in state:
        return None
    blob, sig = state.rsplit(".", 1)
    expect = hmac.new(_sign_secret(), blob.encode(), hashlib.sha256).hexdigest()[:22]
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(blob.encode()).decode())["r"]
    except Exception:  # noqa: BLE001
        return None


@app.api_route("/auth/oauth/start", methods=["GET", "POST"])
def oauth_start(request: Request, redirect: str | None = None, label: str | None = None):
    """Public Google consent URL ('BrainBridge wants access to your Google
    Tasks'). GET -> 302 to Google (button/link in a new tab); POST -> JSON.
    Optional `redirect`: after consent the browser is sent straight to
    <redirect>#brainbridge_key=... so the key reaches the calling app
    without the user ever reading it (silent handback)."""
    packed = _pack_redirect(redirect)
    state = packed or secrets.token_urlsafe(8)
    url = oauth_flow.auth_url(state)
    if request.method == "GET":
        return RedirectResponse(url, status_code=302)
    return {"auth_url": url, "state": state}


@app.get("/oauth2callback", include_in_schema=False)
def oauth2callback(code: str | None = None, error: str | None = None,
                   state: str | None = None):
    """Public: Google redirects here after 'Continue'. We exchange the code,
    store the refresh token (encrypted), and hand out the user's API key."""
    if error:
        return HTMLResponse(_html_msg("❌ Google denied access", f"{error} — You can close this tab."), status_code=400)
    if not code:
        return HTMLResponse(_html_msg("❌ Missing code", "No authorization code received."), status_code=400)
    try:
        tokens = oauth_flow.exchange_code(code)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_html_msg("❌ Could not exchange the code", str(e)[:300]), status_code=400)
    try:
        profile = oauth_flow.user_profile(tokens["access_token"])
    except Exception:  # noqa: BLE001
        profile = {}
    email = profile.get("email", "")
    gsub = profile.get("sub", "")
    # one user per Google account (update refresh token instead of duplicating)
    store = get_store()
    existing = None
    for u in store.list_users():
        if u.get("provider") != "tasks":
            continue
        if (gsub and u.get("google_sub") == gsub) or (email and u.get("email") == email):
            existing = u
            break
    if existing:
        key = existing["key"]
        existing["refresh_token"] = encrypt_state(tokens["refresh_token"].encode())
        if email:
            existing["email"] = email
        if gsub:
            existing["google_sub"] = gsub
        existing["updated"] = int(datetime.now(timezone.utc).timestamp())
        store.put_user(existing)
        label = existing.get("label") or email or "user"
        reused = True
    else:
        key = secrets.token_urlsafe(24)
        label = email or "user"
        store.put_user({
            "key": key, "label": label, "email": email, "google_sub": gsub,
            "provider": "tasks",
            "refresh_token": encrypt_state(tokens["refresh_token"].encode()),
            "created": int(datetime.now(timezone.utc).timestamp()),
            "updated": int(datetime.now(timezone.utc).timestamp()),
            "note": "oauth-tasks",
        })
        reused = False
    client_redirect = _unpack_redirect(state)
    # Verify the freshly issued refresh token actually works (catches
    # 'invalid_grant' right away instead of on the first /memory call).
    verify = None
    try:
        _refresh_and_rotate(store.get_user(key))
    except HTTPException as e:
        verify = str(e.detail)[:200]
    if client_redirect:
        # Silent handback: the user never sees the key — the app receives it.
        qs = f"brainbridge_key={quote(key, safe='')}&email={quote(email, safe='')}"
        if verify:
            qs += f"&oauth_warning={quote(verify, safe='')}"
        return RedirectResponse(f"{client_redirect}#{qs}", status_code=302)
    reused_note = ("<small>(This is <b>the same key</b> as before — every sign-in "
                   "with this Google account reuses it.)</small><br>") if reused else ""
    page = _html_msg(
        "✅ Connected!",
        f"<b>{email or 'Your account'}</b> is linked to BrainBridge.<br>"
        f"{reused_note}"
        f"Your API key:<br><code style='font-size:14px;word-break:break-all'>{key}</code><br><br>"
        f"Send it as <code>Authorization: Bearer {key[:8]}…</code>.<br>"
        + ((f"<br><b style='color:#fbbf24'>⚠ Refresh check failed:</b> "
            f"<small style='color:#fbbf24'>{verify}</small><br>"
            f"<small style='color:#fbbf24'>Revoke BrainBridge at "
            f"myaccount.google.com/permissions and sign in again.</small>"
            if verify else ""))
        + f"<small>Revoke anytime at myaccount.google.com/permissions. "
        f"This key works for /memory/notes, /memory/note, /memory/notes/context.</small>",
    )
    # Popup flow: whisper the key to the opener window (if any) too.
    page = page.replace(
        "</body>",
        f"<script>try{{if(window.opener)window.opener.postMessage("
        f"{{type:'brainbridge_key',key:'{key}',email:'{quote(email, safe='')}'}},'*')}}catch(e){{}}</script></body>",
    )
    return HTMLResponse(page)


def _html_msg(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui;background:#0a0c14;color:#eef0f8;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{max-width:520px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:34px;line-height:1.7}}
h1{{font-size:1.4rem}}code{{background:#161a2c;padding:8px 12px;border-radius:10px;display:inline-block;margin:8px 0}}
small{{color:#9aa7d8}}</style></head><body><div class="card"><h1>{title}</h1><p>{body}</p></div></body></html>"""


@app.post("/auth/oauth/refresh")
def oauth_refresh(authorization: str | None = Header(default=None)):
    """Any key: re-issue the Tasks access token (cheap; used by the guardian)."""
    ctx = _authorize(authorization)
    token = _tasks_token(ctx)
    return {"ok": True, "access_token_len": len(token)}


@app.get("/memory/notes")
def notes_list(limit: int = 300, keyword: str | None = None,
               authorization: str | None = Header(default=None)):
    """BrainBridge Notes: list your memory entries (Google Tasks)."""
    try:
        ctx = _authorize(authorization)
        token = _tasks_token(ctx)
        notes = _gcalls(tasks_store.search_notes, token, keyword, limit=limit) if keyword else _gcalls(tasks_store.list_notes, token, limit=limit)
        return {"brain": "google-tasks", "notes": notes, "count": len(notes)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        raise HTTPException(500, f"notes_list crash: {e}\n{traceback.format_exc()[-1500:]}") from e


@app.post("/memory/note")
def note_save(req: SaveIn, authorization: str | None = Header(default=None)):
    """BrainBridge Notes: save a memory entry (title + content)."""
    try:
        ctx = _authorize(authorization)
        token = _tasks_token(ctx)
        note = _gcalls(tasks_store.create_note, token, req.title, req.content)
        return {"brain": "google-tasks", "note": note}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        raise HTTPException(500, f"note_save crash: {e}\n{traceback.format_exc()[-1500:]}") from e


@app.get("/memory/note/{note_id}")
def note_get(note_id: str, authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    token = _tasks_token(ctx)
    note = _gcalls(tasks_store.get_note, token, note_id)
    if note is None:
        raise HTTPException(404, "no such note")
    return {"brain": "google-tasks", "note": note}


@app.delete("/memory/note/{note_id}")
def note_delete(note_id: str, authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    token = _tasks_token(ctx)
    ok = _gcalls(tasks_store.delete_note, token, note_id)
    return {"ok": ok, "deleted": note_id if ok else None}


@app.get("/memory/notes/context")
def notes_context(limit: int = 8, authorization: str | None = Header(default=None)):
    """BrainBridge Notes: 'what is already known' — ready for an AI prompt."""
    ctx = _authorize(authorization)
    token = _tasks_token(ctx)
    notes = _gcalls(tasks_store.list_notes, token, limit=200)
    context = tasks_store.render_context(notes, limit=limit)
    return {"brain": "google-tasks", "context": context,
            "note_count": len(notes),
            "ai_prompt": ("You are answering from the user's BrainBridge memory "
                          "(Google Tasks). Use the 'context' as authoritative "
                          "background. If it does not answer the question, say so.")}


@app.post("/ask/note")
def note_ask(req: AskIn, authorization: str | None = Header(default=None)):
    """BrainBridge Notes: answer a question from the memory.
    With GEMINI_API_KEY set, a Gemini model answers; otherwise the raw context
    is returned and the calling AI composes the answer."""
    ctx = _authorize(authorization)
    token = _tasks_token(ctx)
    notes = _gcalls(tasks_store.list_notes, token, limit=200)
    context = tasks_store.render_context(notes, limit=10)
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            system = ("You are BrainBridge, the user's permanent memory. "
                      "Answer the question using ONLY the memory below; cite "
                      "entry titles when you use them. If it is not in the "
                      "memory, say that honestly.")
            prompt = system + "\n\n--- MEMORY ---\n" + context + "\n\n--- QUESTION ---\n" + req.question
            r = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash:generateContent",
                params={"key": gemini_key},
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)
            r.raise_for_status()
            cands = r.json().get("candidates", [])
            answer = cands[0]["content"]["parts"][0]["text"] if cands else "(empty)"
            return {"brain": "google-tasks", "answer": answer, "model": "gemini-2.0-flash"}
        except Exception as e:  # noqa: BLE001
            return {"brain": "google-tasks", "answer": None,
                    "context": context, "gemini_error": str(e)[:200]}
    return {"brain": "google-tasks", "answer": None, "context": context,
            "hint": "Set GEMINI_API_KEY on the server to enable automatic answers."}


# ---------------------------------------------------------------------------
# Worker heartbeat + system status (security visibility for the owner & user)
# ---------------------------------------------------------------------------
@app.post("/worker/heartbeat")
def worker_heartbeat(authorization: str | None = Header(default=None)):
    """The hosted-browser worker calls this every ~30s so the /connect page
    can show '🟢 popup online' (and the owner can see it in /system/status)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    try:
        store.put_pending({
            "pending_id": "__hb__",
            "status": "alive",
            "ts": int(datetime.now(timezone.utc).timestamp()),
        })
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:120]}
    return {"ok": True}


def _heartbeat_ts() -> int | None:
    try:
        rec = get_store().get_pending("__hb__")
        return rec.get("ts") if rec else None
    except Exception:  # noqa: BLE001
        return None


@app.get("/system/info")
def system_info():
    """Public (no secrets): is the popup worker online? which store?"""
    ts = _heartbeat_ts()
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "store": store_name(),
        "worker_online": bool(ts and (now - ts) < 150),
        "worker_seen_ago": (now - ts) if ts else None,
        "encryption": "on" if secrets_enabled() else "off",
        "oauth": "on" if os.environ.get("GOOGLE_CLIENT_ID", "").strip() else "off",
    }


@app.get("/system/check")
def system_check():
    """Public diagnostic: is the KV store actually writable/readable, how many
    users exist, and the first chars of their keys (to match a pasted key)."""
    store = get_store()
    out: dict = {"store": store.name, "kv_roundtrip": None,
                 "users_count": None, "user_key_prefixes": []}
    if store.name == "kv":
        import uuid as _uuid
        test = f"bb:diag:{_uuid.uuid4().hex}"
        try:
            store._set(test, "ok")
            ok = store._get(test) == "ok"
            store._del(test)
            out["kv_roundtrip"] = "ok" if ok else "FAIL(readback)"
        except Exception as e:  # noqa: BLE001
            out["kv_roundtrip"] = f"FAIL: {str(e)[:120]}"
    try:
        users = store.list_users()
        out["users_count"] = len(users)
        out["user_key_prefixes"] = sorted({u.get("key", "")[:6] for u in users})[:50]
    except Exception as e:  # noqa: BLE001
        out["users_list_error"] = str(e)[:120]
    return out


@app.get("/system/oauthdebug")
def system_oauthdebug(authorization: str | None = Header(default=None)):
    """Owner: diagnose 'refresh token invalid_grant'. Shows the stored
    refresh token fingerprint, tries a live refresh, and reports exactly
    what Google says. No secrets leaked (token -> last 8 chars only)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    users = [u for u in store.list_users() if u.get("provider") == "tasks"]
    out = {"users": []}
    for u in users:
        rec: dict = {
            "email": u.get("email"),
            "key_prefix": (u.get("key") or "")[:6],
            "created": u.get("created"),
            "updated": u.get("updated"),
            "has_google_sub": bool(u.get("google_sub")),
        }
        try:
            rt = decrypt_state(u["refresh_token"]).decode()
            rec["rt_len"] = len(rt)
            rec["rt_mid"] = rt[:6] + "…" + rt[-8:] if len(rt) > 14 else "?"
        except Exception as e:  # noqa: BLE001
            rec["rt_error"] = str(e)[:120]
            out["users"].append(rec)
            continue
        try:
            at = _refresh_and_rotate(u)
            rec["refresh_ok"] = True
            rec["access_token_len"] = len(at)
            rec["has_prev"] = bool(u.get("refresh_token_prev"))
        except HTTPException as e:
            rec["refresh_ok"] = False
            rec["refresh_err"] = str(e.detail)[:300]
        out["users"].append(rec)
    return out


@app.get("/system/users")
def system_users(authorization: str | None = Header(default=None)):
    """Owner only: full user list (keys visible) so the owner can recover a
    user's key without re-doing OAuth. Refresh tokens are never returned."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    out = []
    for u in store.list_users():
        u = dict(u)
        u.pop("refresh_token", None)
        out.append(u)
    return {"store": store.name, "users": out}


@app.get("/system/status")
def system_status(authorization: str | None = Header(default=None)):
    """Owner: full picture (users, tickets, pending, worker, encryption)."""
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    users = store.list_users()
    tickets = [t for t in store.list_tickets()
               if not str(t.get("ticket_id", "")).startswith(("mint_", "__hb__"))
               and t.get("ticket_id") != "__hb__"]
    pending = [r for r in store.list_pending() if r.get("pending_id") != "__hb__"]
    by_status = {}
    for t in tickets:
        st = t.get("status", "?")
        by_status[st] = by_status.get(st, 0) + 1
    ts = _heartbeat_ts()
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "store": store_name(),
        "encryption": "on" if secrets_enabled() else "off",
        "users": len(users),
        "tickets": by_status,
        "pending_mint": len(pending),
        "worker_online": bool(ts and (now - ts) < 150),
        "worker_seen_ago": (now - ts) if ts else None,
        "keys_issued": len([u for u in users if u.get("key")]),
    }


# ---------------------------------------------------------------------------
# OWNER: user administration
# ---------------------------------------------------------------------------
@app.get("/users")
def users_list(authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    store = get_store()
    out = []
    for u in store.list_users():
        out.append({"key": u["key"], "label": u.get("label", ""),
                    "created": u.get("created"), "updated": u.get("updated"),
                    "note": u.get("note", "")})
    return {"users": out, "count": len(out), "store": store_name()}


@app.delete("/users/{key}")
def user_delete(key: str, authorization: str | None = Header(default=None)):
    ctx = _authorize(authorization)
    if ctx.get("user") is not None:
        raise HTTPException(403, "Owner only")
    ok = get_store().delete_user(key)
    return {"ok": ok, "deleted": key if ok else None}


# ---------------------------------------------------------------------------
# Keepalive (Vercel cron)
# ---------------------------------------------------------------------------
@app.api_route("/cron/keepalive", methods=["GET", "POST"])
def cron_keepalive(request: Request, authorization: str | None = Header(default=None)):
    """Keepalive for Vercel Cron: refresh the owner + every registered user."""
    ua = (request.headers.get("user-agent") or "").lower()
    cron_ok = ua.startswith("vercel-cron")
    key_ok = False
    if authorization and authorization.startswith("Bearer "):
        key_ok = authorization.split(" ", 1)[1].strip() == API_KEY
    if not (cron_ok or key_ok):
        raise HTTPException(403, "cron only")

    result = {"owner": None, "users": {"ok": 0, "fail": 0, "skipped": 0}}
    # owner
    try:
        out = _run(["auth", "refresh"], timeout=120)
        valid, detail = _validate_state(STORAGE)
        result["owner"] = {"ok": valid, "detail": detail[:150]}
    except Exception as e:  # noqa: BLE001
        result["owner"] = {"ok": False, "detail": str(e)[:150]}

    # users (bounded per run)
    store = get_store()
    try:
        users = store.list_users()
    except Exception as e:  # noqa: BLE001
        return {**result, "error": f"store: {e}"}
    for u in users[:40]:
        if _refresh_user(u):
            result["users"]["ok"] += 1
        else:
            result["users"]["fail"] += 1
    result["users"]["skipped"] = max(0, len(users) - 40)
    result["total_users"] = len(users)
    return result


if __name__ == "__main__":
    import uvicorn
    bootstrap_state()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BRAINBRIDGE_GATEWAY_PORT", "8999")))
