# 🧠 BrainBridge Gateway — 3tit l-brain l AI (b NotebookLM)

> L-fikra: **ay AI model** y9der ytwasl m3a l-brain dyalek (Google NotebookLM)
> — ysewwel, yobber memories, y9ra archives. Kolchi kaymchi l NotebookLM.
> Ma-khassek la MCP client la walo: ghir HTTP + key.

## 1) L-gateway (lli khdam daba)

```
Base URL : https://issues-thus-hwy-justice.trycloudflare.com
Key      : ~/.notebooklm/gateway_key.txt  (= `{key[:4]}…{key[-4:]}`)
Auth     : Authorization: Bearer <key>
```
> ⚠️ L-tunnel URL kaytbdel kol ma t-restartiw l-tunnel (chorfo l-log).
> L-Key bqat nafsha. L-gateway f l-machin li kadir: `./run_gateway.sh`.

Jrrbo:
```bash
curl -H "Authorization: Bearer $KEY" https://issues-thus-hwy-justice.trycloudflare.com/status        # session health
curl -H "Authorization: Bearer $KEY" https://issues-thus-hwy-justice.trycloudflare.com/brains        # notebooks dyalk
```

## 2) AI prompt — nss9h 3tih l ay model

```
You have a permanent brain (Google NotebookLM) bridged by BrainBridge.
Base: https://issues-thus-hwy-justice.trycloudflare.com   Auth: Authorization: Bearer <KEY>

- POST {base}/ask {question, brain:"personal"}  -> answer with citations
- POST {base}/memory/save {title, content}      -> persist as dated source (one per session, never overwrite)
- GET  {base}/memory/context                    -> load "what is already known"
- GET  {base}/memory/read?source_id=...         -> read one entry
Before answering from memory: call /ask. At session end: /memory/save conclusions.
```

## 3) Unlock l-session (cookies)

L-session dyal NotebookLM t-expira mn b3d IP change (Token fetch ✗ fail).
Bach n3awdo ncheddo tokens:

1. Installi [Cookie-Editor](https://cookie-editor.com) f Chrome.
2. Sift 3la `https://notebooklm.google.com` → fichier **Export** (JSON).
3. **Importants**: exporti **les deux** :
   - `notebooklm.google.com`
   - `accounts.google.com` (w l-Google SSO domaines li kayniyin)
4. Siftli l-fichier JSON (ola paste f chat).
5. Ana dir:
   ```bash
   notebooklm auth import-cookies /home/user/uploads/cookies.json
   notebooklm auth check --test        # → "Authentication is valid"
   ```
   ola b l-gateway: `POST /auth/import {cookies:[...]}`.

## 4) Keepalive — bqa 3ndek session f les semaines

```bash
nohup ./keepalive.sh >/tmp/nblm-keepalive.log 2>&1 &   # refresh kol 15 min
# ola mn 3nd l-gateway: POST /auth/refresh (same thing)
```

## 5) Endpoints (kollhom b Bearer key)

| Méthode | Endpoint | Ach kaydir |
|---|---|---|
| GET | `/status` | session valide wla la |
| GET | `/brains` | l-notebooks |
| POST | `/ask` `{question, brain}` | sewwel l-brain (citation) |
| POST | `/memory/save` `{title, content, brain}` | sjjl memory (date Markdown) |
| GET | `/memory/list?keyword=&brain=` | search titles |
| GET | `/memory/read?source_id=` | 9ra source kamla |
| GET | `/memory/context` | digest — chno n3rfo deja |
| POST | `/auth/import` `{cookies:[...]}` | unlock session (Cookie-Editor) |
| POST | `/auth/refresh` | rotate token |

Brains: `personal` (Abdelkhalik Brain) / `project` (ArtisanPro Brain).
