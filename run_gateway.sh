#!/usr/bin/env bash
# BrainBridge Gateway — NSB7 l-brain HTTP (ay AI ytwasl m3ah).
# Local:     python3 -m uvicorn brainbridge.gateway:app --host 0.0.0.0 --port 8999
# Public:    bayn it-tunnel (trycloudflare) bach ay AI mn bra ytwasl:
#            bin/cloudflared tunnel --url http://127.0.0.1:8999 --no-autoupdate
set -e
cd "$(dirname "$0")"
echo "==> Gateway http://0.0.0.0:8999 (key: ~/.notebooklm/gateway_key.txt)"
exec python3 -m uvicorn brainbridge.gateway:app --host 0.0.0.0 --port "${BRAINBRIDGE_GATEWAY_PORT:-8999}"
