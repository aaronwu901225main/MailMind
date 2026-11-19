#!/usr/bin/env bash
# Run as a local stdio subprocess of an MCP host, never behind a public reverse proxy.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m app.mcp_server
