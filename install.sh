#!/bin/bash
# Quick setup for memory-cli

AGENTS_DIR="$HOME/workspace/agents"
MEMORY_CLI="$AGENTS_DIR/memory-cli"

echo "🔧 Memory CLI Setup"
echo ""

# Check if memory-cli exists
if [ ! -f "$MEMORY_CLI" ]; then
    echo "❌ Error: memory-cli not found at $MEMORY_CLI"
    exit 1
fi

# Make executable
chmod +x "$MEMORY_CLI"
echo "✓ Made memory-cli executable"

# Check if already in PATH
if echo "$PATH" | grep -q "$AGENTS_DIR"; then
    echo "✓ $AGENTS_DIR already in PATH"
else
    echo ""
    echo "📝 To add memory-cli to your PATH, run:"
    echo ""
    echo "  echo 'export PATH=\"\$HOME/workspace/agents:\$PATH\"' >> ~/.bashrc"
    echo "  source ~/.bashrc"
    echo ""
    echo "Then you can use 'memory' instead of 'memory-cli'"
fi

# Set AGENT_NAME for current shell
echo ""
echo "📝 To set your agent name, add to your shell profile:"
echo ""
echo "  # For Clu (OpenClaw sessions)"
echo "  export AGENT_NAME=clu"
echo ""
echo "  # For agent-a (Claude CLI sessions)"
echo "  export AGENT_NAME=agent-a"
echo ""

# Test the tool
echo "🧪 Testing memory-cli..."
"$MEMORY_CLI" stats

echo ""
echo "✅ Setup complete!"
echo ""
echo "📚 Documentation: ~/workspace/agents/README-memory.md"
echo "🔍 Try: memory query --today"
echo "🔍 Try: memory search \"topic\""
