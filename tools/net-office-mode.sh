#!/usr/bin/env bash
set -euo pipefail

IFACE="${4:-wlo1}"
DOWNLOAD_RATE="${2:-8mbit}"
UPLOAD_RATE="${3:-2mbit}"
ACTION="${1:-status}"

usage() {
  printf 'Usage:\n'
  printf '  %s on [download_rate] [upload_rate] [iface]\n' "$0"
  printf '  %s off [download_rate] [upload_rate] [iface]\n' "$0"
  printf '  %s status [download_rate] [upload_rate] [iface]\n' "$0"
  printf '\nExamples:\n'
  printf '  sudo %s on 8mbit 2mbit wlo1\n' "$0"
  printf '  sudo %s off _ _ wlo1\n' "$0"
}

require_root_for_change() {
  if [[ "$EUID" -ne 0 ]]; then
    printf 'Please run this action with sudo.\n' >&2
    exit 1
  fi
}

apply_limits() {
  require_root_for_change

  # Upload shaping: Token Bucket Filter on outbound traffic.
  tc qdisc replace dev "$IFACE" root handle 1: tbf \
    rate "$UPLOAD_RATE" burst 64kb latency 400ms

  # Download policing: drop excess inbound packets above the target rate.
  # This is intentionally simple and reversible; it avoids IFB setup.
  tc qdisc replace dev "$IFACE" handle ffff: ingress
  tc filter replace dev "$IFACE" parent ffff: protocol all prio 1 u32 \
    match u32 0 0 police rate "$DOWNLOAD_RATE" burst 256kb drop flowid :1

  printf 'Office network mode ON for %s: download=%s upload=%s\n' \
    "$IFACE" "$DOWNLOAD_RATE" "$UPLOAD_RATE"
}

remove_limits() {
  require_root_for_change
  tc qdisc del dev "$IFACE" root 2>/dev/null || true
  tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
  printf 'Office network mode OFF for %s\n' "$IFACE"
}

show_status() {
  printf 'Interface: %s\n\n' "$IFACE"
  printf 'Root qdisc:\n'
  tc qdisc show dev "$IFACE" || true
  printf '\nIngress filters:\n'
  tc filter show dev "$IFACE" parent ffff: || true
}

case "$ACTION" in
  on)
    apply_limits
    ;;
  off)
    remove_limits
    ;;
  status)
    show_status
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
