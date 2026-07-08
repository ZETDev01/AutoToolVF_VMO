#!/usr/bin/env sh
set -eu

if [ -n "${PORT:-}" ] && [ -z "${VINFAST_WEB_PORT:-}" ]; then
  export VINFAST_WEB_PORT="$PORT"
fi

export VINFAST_WEB_HOST="${VINFAST_WEB_HOST:-0.0.0.0}"
export VINFAST_WEB_PORT="${VINFAST_WEB_PORT:-8080}"
export VINFAST_ROBOT_HOST="${VINFAST_ROBOT_HOST:-0.0.0.0}"
export VINFAST_ROBOT_PORT="${VINFAST_ROBOT_PORT:-9000}"

exec python3 main.py
