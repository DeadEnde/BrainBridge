#!/usr/bin/env bash
# BrainBridge — one-command setup (Python deps + Playwright Chromium for login popup)
set -e
cd "$(dirname "$0")"
echo "==> Installing Python dependencies..."
pip3 install --break-system-packages --quiet -r requirements.txt
echo "==> Installing Playwright Chromium (used only for the Google login popup)..."
python3 -m playwright install chromium 2>/dev/null || pip3 install --break-system-packages --quiet 'notebooklm-py[browser]' && python3 -m playwright install chromium
echo "==> Verifying..."
python3 -c "import notebooklm, fastmcp; print('✅ notebooklm + fastmcp OK')"
echo ""
echo "Done. Start with:  ./run.sh   (then call brain_login from your MCP client)"
