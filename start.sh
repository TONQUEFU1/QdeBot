#!/data/data/com.termux/files/usr/bin/bash
#
# QDE V1 - start.sh
#
# Simple launcher for Termux. Run from anywhere with:
#   bash start.sh
# or (after chmod +x start.sh):
#   ./start.sh
#
# Any arguments are passed straight through to main.py, e.g.:
#   ./start.sh --show-rejections
#   ./start.sh --profile fast

set -e

# Always run from the project's own directory, regardless of where
# this script was invoked from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

python main.py "$@"
