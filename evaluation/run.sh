#!/usr/bin/env bash
# ============================================================
# run.sh - Agent security test runner
# ============================================================
#
# Usage:
#   ./run.sh                         # run in foreground (default config.yaml)
#   ./run.sh my_config.yaml          # specify a config file
#   BG=1 ./run.sh                    # run in background, logs to output dir
#   SKIP_BUILD=1 ./run.sh            # skip image build
#
# Environment variables:
#   CONFIG_FILE / $1   config file path (default: config.yaml)
#   SKIP_BUILD=1       skip Docker image build
#   BG=1               run in background (nohup), logs to output/<model>/run.log
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${1:-${CONFIG_FILE:-config.yaml}}"
SKIP_BUILD="${SKIP_BUILD:-0}"
BG="${BG:-0}"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Config file not found: $CONFIG_FILE"
    exit 1
fi

# Read key settings from config.yaml
read_config() {
    python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$CONFIG_FILE'))
key = sys.argv[1]
parts = key.split('.')
v = cfg
for p in parts:
    v = (v or {}).get(p, '')
print(v or '')
" "$1" 2>/dev/null
}

DOCKER_IMAGE="$(read_config docker.image)"
DOCKER_IMAGE="${DOCKER_IMAGE:-aseval:latest}"
TARGET_MODEL="$(read_config target.model)"
MODEL_TAG="${TARGET_MODEL//\//_}"
WORKERS="$(read_config run.workers)"
NUM_RUNS="$(read_config run.num_runs)"

echo "========================================"
echo "  Agent Security Test"
echo "========================================"
echo "  config:   $CONFIG_FILE"
echo "  image:    $DOCKER_IMAGE"
echo "  model:    $TARGET_MODEL"
echo "  workers: $WORKERS"
echo "  num_runs: $NUM_RUNS"
echo "========================================"

# ── Build image ──
if [ "$SKIP_BUILD" != "1" ]; then
    echo ""
    echo "📦 Building Docker image: $DOCKER_IMAGE ..."
    docker build -t "$DOCKER_IMAGE" . --quiet
    echo "✅ Image build complete"
fi

# ── Run ──
echo ""
if [ "$BG" = "1" ]; then
    # Background run
    LOG_DIR="output/${MODEL_TAG}"
    mkdir -p "$LOG_DIR"
    LOG_PATH="${LOG_DIR}/run.log"

    echo "🌙 Running in background, log: ${LOG_PATH}"
    nohup python -u -m src.main "$CONFIG_FILE" > "$LOG_PATH" 2>&1 &
    PID=$!
    echo "   PID=${PID}"
    echo ""
    echo "   View logs: tail -f ${LOG_PATH}"
    echo "   Stop run:  kill ${PID}"
else
    # Foreground run
    python -u -m src.main "$CONFIG_FILE"
fi
