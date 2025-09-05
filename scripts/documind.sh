#!/usr/bin/env bash
# Product: DocuMind AI R2
# File: scripts/documind.sh
# Purpose: Unified launcher for Backend/UI — service or interactive console
# Version: 3.5.2
# Updated: 2025-08-26

# ./scripts/documind.sh
# ./scripts/documind.sh start service both
# ./scripts/documind.sh logs backend
# ./scripts/documind.sh start console backend

set -Eeuo pipefail

### ───────────────────────── CONFIG (edit to match your paths) ─────────────────────────
# Paths (relative/absolute both okay)
# --- edit these at the top of scripts/documind.sh ---
BACKEND_DIR="$HOME/projects/dmai-rc2/dm-ai-r2-server"
FRONTEND_DIR="$HOME/projects/dmai-rc2/dm-ai-r2g-portal"

BACKEND_SERVICE="documind-backend.service"
UI_SERVICE="frontend.service"
VENV_PY="$BACKEND_DIR/venv/bin/python"
BACKEND_PORT="9000"
FRONTEND_PORT="5173"

# Services (use the ones you actually have)
BACKEND_SERVICE="${BACKEND_SERVICE:-documind-backend.service}"   # or: dmai-backend.service
UI_SERVICE="${UI_SERVICE:-frontend.service}"                     # or: documind-ui.service

# EnvFiles (optional)
BACKEND_ENVFILE="${BACKEND_ENVFILE:-/etc/documind/backend.env}"
FRONTEND_ENVFILE="${FRONTEND_ENVFILE:-/etc/documind/frontend.env}"

# Logs (interactive mode tee targets)
LOG_DIR="${LOG_DIR:-$HOME/.local/share/documind/logs}"
mkdir -p "$LOG_DIR"

### ──────────────────────────── helpers / cosmetics ────────────────────────────
c_ok(){ printf "\033[1;32m%s\033[0m\n" "$*"; }
c_info(){ printf "\033[1;34m%s\033[0m\n" "$*"; }
c_warn(){ printf "\033[1;33m%s\033[0m\n" "$*"; }
c_err(){ printf "\033[1;31m%s\033[0m\n" "$*"; }

need(){ command -v "$1" >/dev/null 2>&1 || { c_err "Missing dependency: $1"; exit 1; }; }

port_busy(){ # usage: port_busy 9000
  local p="$1"
  if command -v ss >/dev/null 2>&1; then ss -ltn "( sport = :$p )" | awk 'NR>1{exit 0} END{exit 1}'
  else lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  fi
}

kill_on_port(){ # usage: kill_on_port 9000
  local p="$1"
  if command -v fuser >/dev/null 2>&1; then sudo fuser -k "$p"/tcp || true
  else
    local pid
    pid=$(lsof -ti tcp:"$p" || true)
    [[ -n "${pid:-}" ]] && sudo kill -9 $pid || true
  fi
}

load_envfile(){ # usage: load_envfile /etc/documind/backend.env
  local f="$1"
  [[ -f "$f" ]] && { c_info "Loading env: $f"; set -a; # shellcheck disable=SC1090
  source "$f"; set +a; }
}

### ────────────────────────────── systemd ops ──────────────────────────────
svc_start(){ sudo systemctl start "$1"; }
svc_stop(){ sudo systemctl stop "$1"; }
svc_restart(){ sudo systemctl restart "$1"; }
svc_status(){ sudo systemctl --no-pager status "$1"; }
svc_logs(){ sudo journalctl -u "$1" -n ${2:-200} -f; }

### ────────────────────────────── interactive ops ──────────────────────────────
start_backend_console(){
  load_envfile "$BACKEND_ENVFILE"
  [[ -d "$BACKEND_DIR" ]] || { c_err "Missing BACKEND_DIR: $BACKEND_DIR"; exit 1; }
  need "$VENV_PY"
  if port_busy "$BACKEND_PORT"; then
    c_warn "Port $BACKEND_PORT busy."
    read -r -p "Kill whatever is on $BACKEND_PORT? [y/N] " yn
    [[ "${yn,,}" == "y" ]] && kill_on_port "$BACKEND_PORT"
  fi
  c_info "Starting Backend (console) on :$BACKEND_PORT …"
  cd "$BACKEND_DIR"
  # Tip: drop --reload for prod-like console
  "$VENV_PY" -m uvicorn "$UVICORN_APP" --host 0.0.0.0 --port "$BACKEND_PORT" --reload \
    2>&1 | tee -a "$LOG_DIR/backend.console.log"
}

start_frontend_console(){
  load_envfile "$FRONTEND_ENVFILE"
  [[ -d "$FRONTEND_DIR" ]] || { c_err "Missing FRONTEND_DIR: $FRONTEND_DIR"; exit 1; }
  need npx
  if port_busy "$FRONTEND_PORT"; then
    c_warn "Port $FRONTEND_PORT busy."
    read -r -p "Kill whatever is on $FRONTEND_PORT? [y/N] " yn
    [[ "${yn,,}" == "y" ]] && kill_on_port "$FRONTEND_PORT"
  fi
  c_info "Starting UI (Vite console) on :$FRONTEND_PORT …"
  cd "$FRONTEND_DIR"
  HOST=0.0.0.0 PORT="$FRONTEND_PORT" npx vite --host 0.0.0.0 --port "$FRONTEND_PORT" \
    2>&1 | tee -a "$LOG_DIR/frontend.console.log"
}

### ─────────────────────────────── menus / flows ───────────────────────────────
menu_start_service(){
  select opt in "Backend (service)" "UI (service)" "Both (service)" "Back"; do
    case $REPLY in
      1) c_info "→ systemd start: $BACKEND_SERVICE"; svc_start "$BACKEND_SERVICE"; break ;;
      2) c_info "→ systemd start: $UI_SERVICE"; svc_start "$UI_SERVICE"; break ;;
      3) c_info "→ systemd start: $BACKEND_SERVICE + $UI_SERVICE"; svc_start "$BACKEND_SERVICE"; svc_start "$UI_SERVICE"; break ;;
      4) break ;;
      *) c_warn "Pick 1-4." ;;
    esac
  done
}

menu_stop_service(){
  select opt in "Backend (service)" "UI (service)" "Both (service)" "Back"; do
    case $REPLY in
      1) svc_stop "$BACKEND_SERVICE"; break ;;
      2) svc_stop "$UI_SERVICE"; break ;;
      3) svc_stop "$BACKEND_SERVICE"; svc_stop "$UI_SERVICE"; break ;;
      4) break ;;
      *) c_warn "Pick 1-4." ;;
    esac
  done
}

menu_logs_service(){
  select opt in "Backend logs" "UI logs" "Back"; do
    case $REPLY in
      1) svc_logs "$BACKEND_SERVICE"; break ;;
      2) svc_logs "$UI_SERVICE"; break ;;
      3) break ;;
      *) c_warn "Pick 1-3." ;;
    esac
  done
}

menu_console(){
  select opt in "Backend (console w/ live logs)" "UI (console w/ live logs)" "Back"; do
    case $REPLY in
      1) start_backend_console; break ;;
      2) start_frontend_console; break ;;
      3) break ;;
      *) c_warn "Pick 1-3." ;;
    esac
  done
}

top_menu(){
  PS3=$'\n'"Select: "
  c_ok "DocuMind Launcher — service or console"
  select opt in \
    "Start (service)" \
    "Stop (service)" \
    "Restart (service)" \
    "Status (service)" \
    "Logs (service)" \
    "Start (interactive console)" \
    "Quit"; do
    case $REPLY in
      1) menu_start_service ;;
      2) menu_stop_service ;;
      3) c_info "Restarting services…"; menu_stop_service; menu_start_service ;;
      4) c_info "Service status:"; svc_status "$BACKEND_SERVICE" || true; echo; svc_status "$UI_SERVICE" || true ;;
      5) menu_logs_service ;;
      6) menu_console ;;
      7) break ;;
      *) c_warn "Pick 1-7." ;;
    esac
  done
}

### ──────────────────────────────── CLI (optional) ────────────────────────────────
usage(){
cat <<EOF
Usage:
  $0                # interactive menu
  $0 start service backend|ui|both
  $0 stop service backend|ui|both
  $0 restart service backend|ui|both
  $0 status
  $0 logs backend|ui
  $0 start console backend|ui
EOF
}

case "${1:-}" in
  "") top_menu ;;
  start)
    case "${2:-}" in
      service)
        case "${3:-}" in
          backend) svc_start "$BACKEND_SERVICE" ;;
          ui) svc_start "$UI_SERVICE" ;;
          both) svc_start "$BACKEND_SERVICE"; svc_start "$UI_SERVICE" ;;
          *) usage; exit 1 ;;
        esac ;;
      console)
        case "${3:-}" in
          backend) start_backend_console ;;
          ui) start_frontend_console ;;
          *) usage; exit 1 ;;
        esac ;;
      *) usage; exit 1 ;;
    esac ;;
  stop)
    [[ "${2:-}" == "service" ]] || { usage; exit 1; }
    case "${3:-}" in
      backend) svc_stop "$BACKEND_SERVICE" ;;
      ui) svc_stop "$UI_SERVICE" ;;
      both) svc_stop "$BACKEND_SERVICE"; svc_stop "$UI_SERVICE" ;;
      *) usage; exit 1 ;;
    esac ;;
  restart)
    [[ "${2:-}" == "service" ]] || { usage; exit 1; }
    case "${3:-}" in
      backend) svc_restart "$BACKEND_SERVICE" ;;
      ui) svc_restart "$UI_SERVICE" ;;
      both) svc_restart "$BACKEND_SERVICE"; svc_restart "$UI_SERVICE" ;;
      *) usage; exit 1 ;;
    esac ;;
  status)
    svc_status "$BACKEND_SERVICE" || true; echo; svc_status "$UI_SERVICE" || true ;;
  logs)
    case "${2:-}" in
      backend) svc_logs "$BACKEND_SERVICE" ;;
      ui) svc_logs "$UI_SERVICE" ;;
      *) usage; exit 1 ;;
    esac ;;
  *) usage; exit 1 ;;
esac
