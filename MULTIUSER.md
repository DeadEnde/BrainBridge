# BrainBridge Multi — kola user 3ndo l-brain dyalo

L-gateway daba **multi-tenant**: kola user kay-connecti session dyalo → kayakhod
**API key privée** → y-3tiha l'ay AI model. Les sessions kay-b9aw 7ayyin b
keepalive automatique.

## 🔄 L-flux dyal user

```
user → /connect (site) → paste cookies  wla  managed login (hosted browser)
     → l-gateway kay-validate l-session m3a Google
     → kay-3tih API key privée (secrets.token_urlsafe, 32 chars)
     → user kay-3ti l-key l'ay AI:  POST /ask  +  Authorization: Bearer <key>
```

Kola key = kola session = kola Google compte (machi mshtarka).

## 🧬 Storage auto-detect (`brainbridge/users_store.py`)

| Backend | Env vars | Kifach |
|---|---|---|
| **Upstash KV** (recommandé) | `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Vercel → Storage → Create Database → Upstash Redis (free) → Connect to project (les env vars kay-zadou automatiquement) |
| **Vercel Blob** | `BLOB_READ_WRITE_TOKEN` | Vercel → Storage → Create → Blob (free) → Connect |
| **File** (dev only) | — | `data/users/*.json` f repo (gitignored) — 3la Vercel kayn only f /tmp → **ma y-persistach** |

> ⚠️ 3la Vercel: khassek **KV wla Blob** bach l-users yb9aw. Bla, l-register kay-khedem
> walakin l-users kay-tmso mn b3d cold start.

## 🔑 Endpoints jdad

| Endpoint | Auth | Description |
|---|---|---|
| `POST /api/auth/register` | public | paste `cookies` (Cookie-Editor) / `state` / `state_b64` → `{api_key}` |
| `POST /api/auth/tickets` | public | create managed-login ticket |
| `GET /api/auth/tickets/{id}` | public | poll → `{status, browser_url, api_key}` |
| `GET /api/auth/tickets/next` | owner | worker: pick oldest pending ticket |
| `POST /api/auth/tickets/{id}/url` | owner | worker: set hosted-browser URL + password |
| `POST /api/auth/tickets/{id}/collect` | owner | worker: upload captured session → user's key |
| `POST /api/auth/tickets/{id}/fail` | owner | mark failed |
| `GET /api/users` | owner | list users |
| `DELETE /api/users/{key}` | owner | remove a user |
| `POST /api/auth/refresh` | any key | rotate token for THAT key |
| `GET /api/cron/keepalive` | cron/owner | refresh owner + kolchi users (40/run) |

L-endpoints l-9dam (`/ask`, `/memory/*`, `/status`, `/brains`) khdamim b ay key —
kola key kay-wassel l-session dyalo.

## 🖥️ Hosted browser worker (`tools/login_worker.py`)

Kay-khedem 3la machine b display (sandbox dyalna / VPS):

```bash
export BRAINBRIDGE_BASE="https://brain-bridge-six.vercel.app/api"
export BRAINBRIDGE_KEY="<owner key>"
export DISPLAY=":99"
python3 tools/login_worker.py
```

Kola ticket: kay-7ell Chromium (notebooklm login) 3la l-display → user kay-dkhel
Google f l-VNC → worker kay-capturi l-session → upload → user yakhod l-key.

## 🧪 Test local

```bash
uvicorn api.index:app --app-dir . --port 8999
# → http://127.0.0.1:8999/connect
```

## ⚠️ Security

- L-cookies dyal users = **srari** → `data/` gitignored, f KV/Blob b tokens privés.
- Owner key = master (kay-chouf les users). User keys = access ghir l-session dyahom.
- `__Secure-1PSIDTS` ma kaynch f exports dyal Cookie-Editor (HttpOnly) → paste
  kay-7taj l-export tkon mn notebook.google.com logged-in; managed login khass
  ykoun 100% fiable.
