#!/bin/bash
# Development launch script for Open Voice Input Linux.
# Usage: ./run.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Add src to PYTHONPATH
export PYTHONPATH="$SCRIPT_DIR/src:$PYTHONPATH"

echo "🎤 Starting Open Voice Input Linux..."
python3 -m doubao_murmur "$@"
