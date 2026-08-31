# 🚀 BrainBridge — deploy 3la Vercel

L-gateway kaykhdem f Vercel b serverless Python (nafs pattern dyal CVForge):
- `api/index.py` = l-function (kay-mounti l-gateway ta7t `/api/*`)
- `vercel.json` = headers + `maxDuration: 60` + **cron keepalive** (kol nhar 03:17 UTC — 3la Pro beddlo l `"*/15 * * * *"`)
- `requirements.txt` = deps (notebooklm-py, fastapi, ...)

## Setup f Vercel (marra wa7da)

1. **Import l-repo**: Vercel → **Add New Project → Import `DeadEnde/BrainBridge`**
   (ila ma-baynch: GitHub → Settings → Install bach Vercel ychof l-repo).
2. **Env Variables** (Settings → Environment Variables):
   | Var | Ach howa | Fin nji bih |
   |---|---|---|
   | `BRAINBRIDGE_GATEWAY_KEY` | l-clef li kay-siftou les clients (ex: `Bearer X..`) | walo dirha b rasek |
   | `BRAINBRIDGE_STATE_B64` | session NotebookLM b base64 | `GET /api/auth/export` (mn b3d import) |
   | `BRAINBRIDGE_STORAGE` (optional) | `/tmp/storage_state.json` | default on Vercel |

3. **Deploy** → l-project kayntaj `https://brainbridge.vercel.app` (wla l-URL li bghiti).

## Kifach l-session t-surviva (cold starts)

Vercel = disk ephemeral → n-verifiw l-session mn env var:
1. Importi cookies (Cookie-Editor export) → `POST /api/auth/import` (ola CLI lokal `notebooklm auth import-cookies`).
2. Khod l-resultat → `GET /api/auth/export` → `state_b64`.
3. 7etto f `BRAINBRIDGE_STATE_B64` f Vercel env. (L'key kay-bqa.)
4. `GET /api/status` → `valid: true`.

> ℹ️ L-endpoint `/auth/import` w `/auth/refresh` kay-rja3o `state_b64` f l-response — pastih f env bech la tnsa.

## Cron keepalive

- Hobby: marra f nhar (limit) — ay mzyan b7al ma l-token kay-bqa ~semana.
- Pro: beddel `vercel.json` → `"schedule": "*/15 * * * *"` (nafs l-cadence dyal keepalive dyana).
- L-endpoint m-protégé: kay-9bel ghir `User-Agent: vercel-cron/*` ola Bearer key.

## Test mn b3d l-deploy

```bash
KEY="<dyalk>"; B="https://brainbridge.vercel.app"
curl -H "Authorization: Bearer $KEY" $B/api/status          # valid: true/false
curl -H "Authorization: Bearer $KEY" $B/api/brains          # notebooks
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"question":"what do you know about me?","brain":"personal"}' $B/api/ask
```

## Notes

- L-gateway f Vercel = public internet + Bearer key → **ma-t7atach l-key f code/GitHub**, ghir env vars.
- `auth import` kay-n7et l-session f `/tmp` (ephemeral) — l-durable 3ndek f env var, w lokal f `~/.notebooklm/`.
- Local (sandbox/machine dyalk): nafs l-kod `./run_gateway.sh` + `bin/cloudflared tunnel --url http://127.0.0.1:8999` → URL pública (trycloudflare) ila bghiti haja urgent bla Vercel.
