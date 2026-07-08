#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export VINFAST_WEB_HOST="${VINFAST_WEB_HOST:-0.0.0.0}"
export VINFAST_ROBOT_HOST="${VINFAST_ROBOT_HOST:-0.0.0.0}"
export VINFAST_RESTART_EXIT_CODE="${VINFAST_RESTART_EXIT_CODE:-42}"

realtime_base_url="${VINFAST_REALTIME_BASE_URL:-wss://groot.vizone.ai/api/v2/s2s}"
realtime_probe_base_url="${realtime_base_url/#wss:/https:}"
realtime_probe_base_url="${realtime_probe_base_url/#ws:/http:}"
realtime_model="${VINFAST_REALTIME_MODEL:-vsf}"
realtime_device_id="${VINFAST_REALTIME_DEVICE_ID:-robot_03072026_official_qcd}"
realtime_probe_url="${realtime_probe_base_url%/}/realtime?model=${realtime_model}&device_id=${realtime_device_id}"

proxy_auth_location() {
  curl -sS -I "$realtime_probe_url" 2>/dev/null \
    | awk 'BEGIN{IGNORECASE=1} /^Location:/ {sub(/\r$/, ""); sub(/^Location:[[:space:]]*/, ""); print; exit}'
}

maybe_auth_company_proxy() {
  local auth_url
  auth_url="$(proxy_auth_location || true)"
  if [ -z "$auth_url" ] || [[ "$auth_url" != *"mwg-internal"* ]]; then
    return 0
  fi

  echo ">>> Company proxy auth:   required"
  echo ">>> Auth URL:             ${auth_url}"
  if [ -n "${VINFAST_PROXY_USERNAME:-}" ]; then
    python3 scripts/proxy_auth.py "$auth_url" "$VINFAST_PROXY_USERNAME" || true
  else
    echo ">>> Set VINFAST_PROXY_USERNAME, then rerun script to authenticate proxy before realtime."
  fi
}

find_free_port() {
  local preferred="$1"
  python3 - "$preferred" <<'PY'
import socket
import sys

preferred = int(sys.argv[1])
for port in [preferred, *range(preferred + 1, preferred + 50)]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        print(port)
        raise SystemExit(0)
raise SystemExit(f"No free port near {preferred}")
PY
}

if [ -z "${VINFAST_WEB_PORT:-}" ]; then
  VINFAST_WEB_PORT="$(find_free_port 8080)"
fi
if [ -z "${VINFAST_ROBOT_PORT:-}" ]; then
  VINFAST_ROBOT_PORT="$(find_free_port 9000)"
fi
export VINFAST_WEB_PORT VINFAST_ROBOT_PORT

lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

print_urls() {
  echo ">>> VinFast test UI local: http://127.0.0.1:${VINFAST_WEB_PORT}/"
  if [ -n "$lan_ip" ]; then
    echo ">>> VinFast test UI LAN:   http://${lan_ip}:${VINFAST_WEB_PORT}/"
  fi
  echo ">>> Health check:          http://127.0.0.1:${VINFAST_WEB_PORT}/health"
  echo ">>> Robot TCP:             ${VINFAST_ROBOT_HOST}:${VINFAST_ROBOT_PORT}"
}

probe_server() {
  local tries=30
  while [ "$tries" -gt 0 ]; do
    if curl -fsS "http://127.0.0.1:${VINFAST_WEB_PORT}/health" >/dev/null 2>&1; then
      echo ">>> Health probe OK"
      return 0
    fi
    tries=$((tries - 1))
    sleep 0.5
  done
  echo ">>> Health probe failed; server is still attached below for logs."
  return 1
}

while true; do
  maybe_auth_company_proxy
  print_urls
  python3 main.py &
  server_pid="$!"
  probe_server || true
  wait "$server_pid"
  exit_code="$?"
  if [ "$exit_code" != "$VINFAST_RESTART_EXIT_CODE" ]; then
    exit "$exit_code"
  fi
  echo ">>> Server requested restart; starting again."
done
