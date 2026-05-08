#!/usr/bin/env bash
# ============================================================
# run.sh - 平台安全测试运行器
# ============================================================
#
# 用法:
#   ./run.sh                         # 前台运行 (默认 config.yaml)
#   ./run.sh my_config.yaml          # 指定配置文件
#   BG=1 ./run.sh                    # 后台运行，日志写到 output 目录
#   SKIP_BUILD=1 ./run.sh            # 跳过镜像构建
#
# 环境变量:
#   CONFIG_FILE / $1   配置文件路径 (默认: config.yaml)
#   SKIP_BUILD=1       跳过 Docker 镜像构建
#   BG=1               后台运行 (nohup)，日志写到 output/<model>/run.log
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${1:-${CONFIG_FILE:-config.yaml}}"
SKIP_BUILD="${SKIP_BUILD:-0}"
BG="${BG:-0}"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

# 从 config.yaml 读取关键配置
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
DOCKER_IMAGE="${DOCKER_IMAGE:-agent3sigma-stage:latest}"
TARGET_MODEL="$(read_config target.model)"
MODEL_TAG="${TARGET_MODEL//\//_}"
WORKERS="$(read_config run.workers)"
NUM_RUNS="$(read_config run.num_runs)"

echo "========================================"
echo "  Agent Security Test"
echo "========================================"
echo "  配置:   $CONFIG_FILE"
echo "  镜像:   $DOCKER_IMAGE"
echo "  模型:   $TARGET_MODEL"
echo "  workers: $WORKERS"
echo "  num_runs: $NUM_RUNS"
echo "========================================"

# ── 构建镜像 ──
if [ "$SKIP_BUILD" != "1" ]; then
    echo ""
    echo "📦 构建 Docker 镜像: $DOCKER_IMAGE ..."
    docker build -t "$DOCKER_IMAGE" . --quiet
    echo "✅ 镜像构建完成"
fi

# ── 运行 ──
echo ""
if [ "$BG" = "1" ]; then
    # 后台运行
    LOG_DIR="output/${MODEL_TAG}"
    mkdir -p "$LOG_DIR"
    LOG_PATH="${LOG_DIR}/run.log"

    echo "🌙 后台运行，日志: ${LOG_PATH}"
    nohup python -u -m src.main "$CONFIG_FILE" > "$LOG_PATH" 2>&1 &
    PID=$!
    echo "   PID=${PID}"
    echo ""
    echo "   查看日志:  tail -f ${LOG_PATH}"
    echo "   停止运行:  kill ${PID}"
else
    # 前台运行
    python -u -m src.main "$CONFIG_FILE"
fi
