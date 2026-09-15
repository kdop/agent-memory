#!/bin/bash
# Quick setup for agent-memory. Run from a clone of this repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEMORY_CLI="$REPO_DIR/memory-cli"

echo "🔧 agent-memory setup"
echo ""

if [ ! -f "$MEMORY_CLI" ]; then
    echo "❌ memory-cli not found at $MEMORY_CLI"
    exit 1
fi
chmod +x "$MEMORY_CLI"

# Two ways to get `memory-cli` on PATH — pick whichever you prefer.
echo "Install options:"
echo ""
echo "  A) pip (recommended) — installs the memory-cli console script:"
echo "       pip install -e \"$REPO_DIR\""
echo "     Optional surfaces:  pip install -e \"$REPO_DIR\"[server]   # HTTP API"
echo "                         pip install -e \"$REPO_DIR\"[mcp]      # MCP server"
echo ""
echo "  B) shim on PATH + alias (no install; client is stdlib-only):"
echo "       export PATH=\"$REPO_DIR:\$PATH\""
echo "       alias memory=\"$MEMORY_CLI\""
echo "     Add those two lines to your shell profile to persist them."
echo ""
echo "Set your agent name in your shell profile, e.g.:"
echo "       export AGENT_NAME=my-agent"
echo ""

# Smoke test: the client is stdlib-only, so --help must work with nothing installed
# and no server running.
echo "🧪 Testing memory-cli..."
"$MEMORY_CLI" --help >/dev/null && echo "✓ memory-cli runs"

echo ""
echo "✅ Setup info printed."
echo "📚 Docs: README.md (usage), MEMORY.md (logging protocol), ARCHITECTURE.md (design)"
echo "🔍 Try:  memory query --since-days 0   |   memory search \"topic\""
