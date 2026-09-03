"""BrainBridge Gateway — Vercel serverless entry.

Mounts the gateway app under /api/* (same pattern as the CVForge API).
On boot it restores the NotebookLM session from BRAINBRIDGE_STATE_B64 (env),
so the brain keeps working across cold starts.

Vercel env vars (Settings -> Environment Variables):
  BRAINBRIDGE_GATEWAY_KEY   -> the bearer key clients send (Authorization: Bearer ...)
  BRAINBRIDGE_STATE_B64     -> base64 of storage_state.json (run GET /api/auth/export
                               after a successful /api/auth/import to get it)
Optional:
  BRAINBRIDGE_STORAGE       -> defaults to /tmp/storage_state.json on Vercel

Local test of the same entrypoint:
  uvicorn api.index:app --app-dir . --port 8999
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Vercel executes the function from inside /var/task/api and does NOT put the
# repo root on sys.path — make `import brainbridge` resolvable before anything.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse
from pathlib import Path as _P

# On Vercel /tmp is writable; keep the session there (ephemeral by design —
# the durable copy lives in BRAINBRIDGE_STATE_B64).
if os.environ.get("VERCEL"):
    os.environ.setdefault("BRAINBRIDGE_STORAGE", "/tmp/storage_state.json")

from brainbridge.gateway import app as gateway_app  # noqa: E402
from brainbridge.gateway import bootstrap_state  # noqa: E402

bootstrap_state()

app = FastAPI(title="BrainBridge (Vercel)", version="1.0")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root_page():
    """Landing/status page — so the root URL is not a bare 404."""
    p = _P(__file__).parent / "root.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>BrainBridge Gateway</h1><p>API at <code>/api/*</code></p>")


@app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
def connect_page():
    """Multi-user connect page (paste export + managed login)."""
    p = _P(__file__).parent / "connect.html"
    if p.exists():
        return HTMLResponse(
            p.read_text(encoding="utf-8").replace("{{API_BASE}}", "/api")
        )
    return HTMLResponse("<h1>BrainBridge Connect</h1><p>POST /api/auth/register</p>")


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_page():
    """Privacy policy — required by Google OAuth verification."""
    p = _P(__file__).parent / "privacy.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>BrainBridge Privacy Policy</h1>")


app.mount("/api", gateway_app)
