#!/usr/bin/env bash
# ============================================================
# stop.sh - Stop running Agent security tests
# ============================================================
#
# Usage:
#   ./stop.sh          # stop all test processes + clean up leftover containers
#   ./stop.sh --force  # force kill -9
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

FORCE="${1:-}"

echo "📅 $(date '+%F %H:%M:%S')"
echo ""

# ── 1. Find test processes ──
pids=$(ps -ef | grep "[s]rc.main" | awk '{print $2}')

if [ -z "$pids" ]; then
    echo "  (no test main process running)"
else
    echo "=== 🔧 Test processes ==="
    ps -ef | grep "[s]rc.main"
    echo ""

    for pid in $pids; do
        # Also find its child processes (workers)
        children=$(pgrep -P "$pid" 2>/dev/null || true)
        all_pids="$pid $children"

        if [ "$FORCE" = "--force" ]; then
            echo "  kill -9 $all_pids"
            kill -9 $all_pids 2>/dev/null || true
        else
            echo "  kill $all_pids"
            kill $all_pids 2>/dev/null || true
        fi
    done

    sleep 1
    remaining=$(ps -ef | grep "[s]rc.main" | awk '{print $2}')
    if [ -n "$remaining" ]; then
        echo "  ⚠️  Processes still alive, trying kill -9..."
        kill -9 $remaining 2>/dev/null || true
    fi
    echo "  ✅ Test processes stopped"
fi

echo ""

# ── 2. Clean up leftover containers ──
echo "=== 🐳 Cleaning up aseval containers ==="
containers=$(docker ps -q --filter 'name=aseval-' 2>/dev/null || true)

if [ -z "$containers" ]; then
    echo "  (no leftover containers)"
else
    count=$(echo "$containers" | wc -l | tr -d ' ')
    echo "  Found $count containers, cleaning up..."
    docker rm -f $containers 2>/dev/null || true
    echo "  ✅ Containers cleaned up"
fi

echo ""
echo "Done."
