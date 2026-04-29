#!/usr/bin/env bash
# One-command dev launcher for the whole debate stack.
# Usage: ./scripts/run_all.sh

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill in keys." >&2
  exit 1
fi

if ! command -v honcho >/dev/null 2>&1; then
  echo "honcho is not installed. Install the dev extras first:"
  echo "  uv pip install -e \".[dev]\""
  echo "or just install honcho alone:"
  echo "  uv pip install honcho"
  exit 1
fi

exec honcho start
