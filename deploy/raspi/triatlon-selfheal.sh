#!/usr/bin/env bash
set -euo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
APP_HEALTH_URL="http://127.0.0.1/healthz"
APP_PUBLIC_URL="http://127.0.0.1/film-est-sorsolo"
FUNNEL_TARGET="http://127.0.0.1:80"
FUNNEL_PORT="80"

log() {
  logger -t triatlon-selfheal "$1"
  echo "$1"
}

is_http_ok() {
  local url="$1"
  local status
  status="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 "$url" || true)"
  [[ "$status" == "200" ]]
}

ensure_services() {
  local changed=0

  if ! systemctl is-active --quiet tailscaled; then
    log "tailscaled service not active, restarting"
    systemctl restart tailscaled
    changed=1
  fi

  if ! systemctl is-active --quiet triatlon; then
    log "triatlon service not active, restarting"
    systemctl restart triatlon
    changed=1
  fi

  if ! systemctl is-active --quiet nginx; then
    log "nginx service not active, restarting"
    systemctl restart nginx
    changed=1
  fi

  if [[ "$changed" == "1" ]]; then
    sleep 3
  fi
}

ensure_app_health() {
  if ! is_http_ok "$APP_HEALTH_URL"; then
    log "health endpoint failed, restarting triatlon and nginx"
    systemctl restart triatlon
    systemctl restart nginx
    sleep 2
  fi

  if ! is_http_ok "$APP_PUBLIC_URL"; then
    log "public page failed locally, restarting triatlon and nginx"
    systemctl restart triatlon
    systemctl restart nginx
    sleep 2
  fi
}

ensure_tailscale_ready() {
  local waited=0
  while [[ "$waited" -lt 20 ]]; do
    if tailscale status >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log "tailscale status is unavailable after wait; restarting tailscaled"
  systemctl restart tailscaled
  sleep 3
}

ensure_funnel() {
  local status
  status="$(tailscale funnel status 2>/dev/null || true)"

  if [[ "$status" != *"https://"* ]] || [[ "$status" != *"$FUNNEL_TARGET"* ]]; then
    log "funnel missing or wrong target, reconfiguring"
    tailscale funnel reset >/dev/null 2>&1 || true
    tailscale funnel --bg "$FUNNEL_PORT" >/dev/null
    sleep 2
    status="$(tailscale funnel status 2>/dev/null || true)"
    if [[ "$status" != *"https://"* ]] || [[ "$status" != *"$FUNNEL_TARGET"* ]]; then
      log "funnel still not healthy after reconfigure"
    fi
  fi
}

main() {
  ensure_services
  ensure_app_health
  ensure_tailscale_ready
  ensure_funnel
}

main "$@"
