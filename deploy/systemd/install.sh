#!/usr/bin/env bash
# Idempotent installer for the Julia systemd unit.
#
# Usage:
#   sudo ./deploy/systemd/install.sh install
#   sudo ./deploy/systemd/install.sh uninstall
#
# What install does:
#   1. Ensures a `julia` user exists (no shell, no home).
#   2. Drops /etc/systemd/system/julia.service from the repo copy.
#   3. Drops /etc/julia/julia.env if missing (loads JULIA_* env vars).
#   4. Reloads systemd, enables + starts the unit, prints status.
#
# What uninstall does:
#   1. Stops + disables the unit.
#   2. Removes /etc/systemd/system/julia.service.
#
# Both paths are idempotent: running install twice is a no-op on
# the second pass, and uninstall on a never-installed host exits
# cleanly with a message instead of erroring.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_SRC="${REPO_DIR}/deploy/systemd/julia.service"
UNIT_DST="/etc/systemd/system/julia.service"
ENV_DST="/etc/julia/julia.env"

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
  fi
}

ensure_user() {
  if ! id -u julia >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin julia
    echo "Created system user 'julia'."
  fi
}

write_unit() {
  install -d -m 0755 "$(dirname "$UNIT_DST")"
  install -m 0644 "$UNIT_SRC" "$UNIT_DST"
  echo "Installed unit: $UNIT_DST"
}

write_env_template() {
  if [[ ! -f "$ENV_DST" ]]; then
    install -d -m 0750 "$(dirname "$ENV_DST")"
    cat >"$ENV_DST" <<'EOF'
# /etc/julia/julia.env — sourced by julia.service at start.
# All JULIA_ env vars listed in src/julia/config.py are honoured.
# Required for live operation:
#   JULIA_JULES_API_KEY=...
#   JULIA_GITHUB_TOKEN=...
#   JULIA_DEFAULT_REPO=owner/name
# Optional:
#   JULIA_BEHAVIORS_REPO=owner/name
#   JULIA_TELEGRAM_BOT_TOKEN=...
#   JULIA_TELEGRAM_CHAT_ID=...
#   JULIA_HEARTBEAT_URL=https://hc-ping.com/...
EOF
    chmod 0640 "$ENV_DST"
    chown root:julia "$ENV_DST" 2>/dev/null || true
    echo "Wrote env template: $ENV_DST (edit it with credentials, then restart)."
  fi
}

case "${1:-}" in
  install)
    require_root
    ensure_user
    write_unit
    write_env_template
    systemctl daemon-reload
    systemctl enable --now julia.service
    systemctl --no-pager status julia.service
    ;;
  uninstall)
    require_root
    systemctl disable --now julia.service 2>/dev/null || true
    rm -f "$UNIT_DST"
    systemctl daemon-reload
    echo "Uninstalled: $UNIT_DST"
    ;;
  *)
    echo "Usage: $0 {install|uninstall}" >&2
    exit 2
    ;;
esac
