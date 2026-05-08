#!/usr/bin/env bash
# ============================================================
# stop.sh - 停止正在运行的平台安全测试
# ============================================================
#
# 用法:
#   ./stop.sh          # 停止所有测试进程 + 清理残留容器
#   ./stop.sh --force  # 强制 kill -9
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

FORCE="${1:-}"

echo "📅 $(date '+%F %H:%M:%S')"
echo ""

# ── 1. 查找测试进程 ──
pids=$(ps -ef | grep "[s]rc.main" | awk '{print $2}')

if [ -z "$pids" ]; then
    echo "  (无测试主进程在跑)"
else
    echo "=== 🔧 测试进程 ==="
    ps -ef | grep "[s]rc.main"
    echo ""

    for pid in $pids; do
        # 同时找出它的子进程（worker）
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
        echo "  ⚠️  进程仍存活，尝试 kill -9..."
        kill -9 $remaining 2>/dev/null || true
    fi
    echo "  ✅ 测试进程已停止"
fi

echo ""

# ── 2. 清理残留容器 ──
echo "=== 🐳 清理 agent3sigma 容器 ==="
containers=$(docker ps -q --filter 'name=agent3sigma-' 2>/dev/null || true)

if [ -z "$containers" ]; then
    echo "  (无残留容器)"
else
    count=$(echo "$containers" | wc -l | tr -d ' ')
    echo "  发现 $count 个容器，正在清理..."
    docker rm -f $containers 2>/dev/null || true
    echo "  ✅ 容器已清理"
fi

echo ""
echo "Done."
