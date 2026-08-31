#!/usr/bin/env bash
# BrainBridge — start the MCP server (stdio transport)
set -e
cd "$(dirname "$0")"
exec python3 -m brainbridge "$@"
