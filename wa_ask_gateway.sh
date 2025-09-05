#!/usr/bin/env bash
# wa_ask_gateway.sh — manage WhatsApp → Ask DocuMind gateway
# Usage: ./wa_ask_gateway.sh start|stop|restart|status|logs|health

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

VENV_BIN="$SCRIPT_DIR/venv/bin"
UVICORN_VENV="$VENV_BIN/uvicorn"

RUN_DIR="$SCRIPT_DIR/run";   mkdir -p "$RUN_DIR"
LOG_DIR="$SCRIPT_DIR/logs";  mkdir -p "$LOG_DIR"

PID_FILE="$RUN_DIR/wa_ask_gateway.pid"
LOG_FILE="$LOG_DIR/wa_ask_gateway.out"

# Load .env if present (for ASK_API_URL, FORWARD_TIMEOUT_S, GATEWAY_PORT, etc.)
if [[ -f ".env" ]] || [[ -f ".env.hr" ]]; then
  # prefer .env.hr if present
  if [[ -f ".env.hr" ]]; then
    set +u; set -a; source .env.hr; set +a; set -u
  else
    set +u; set -a; source .env; set +a; set -u
  fi
fi

GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"
GATEWAY_PORT="${GATEWAY_PORT:-8011}"   # default if not in .env

export ASK_API_URL="${ASK_API_URL:-http://127.0.0.1:9000/api/wa/webhook}"
export FORWARD_TIMEOUT_S="${FORWARD_TIMEOUT_S:-12}"

if [[ -x "$UVICORN_VENV" ]]; then
  UVICORN="$UVICORN_VENV"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="$(command -v uvicorn)"
else
  echo "❗ uvicorn not found. Activate venv and install deps:"
  echo "   source venv/bin/activate"
  echo "   pip install fastapi 'uvicorn[standard]' requests python-multipart"
  exit 1
fi

CMD_BASE="$UVICORN whatsapp_ask_gateway:app --host $GATEWAY_HOST --port $GATEWAY_PORT --workers 1 --log-level info"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" && -d "/proc/$pid" ]] || return 1
  ps -o cmd= -p "$pid" 2>/dev/null | grep -q "uvicorn .*whatsapp_ask_gateway:app .*--port $GATEWAY_PORT"
}

port_in_use_by_other() {
  local pids; pids="$(ss -ltnp 2>/dev/null | awk -v port=":$GATEWAY_PORT" '$4 ~ port {print $6}' | sed -E 's/.*pid=([0-9]+).*/\1/')" || true
  [[ -z "$pids" ]] && return 1
  if [[ -f "$PID_FILE" ]]; then
    local mypid; mypid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ -n "$mypid" ]] && pids="$(echo "$pids" | tr ' ' '\n' | grep -vx "$mypid" || true)"
  fi
  [[ -n "$pids" ]]
}

start() {
  if is_running; then
    echo "✅ Ask gateway already running (pid $(cat "$PID_FILE")) on :$GATEWAY_PORT"
    return 0
  fi
  if port_in_use_by_other; then
    echo "❗ Port $GATEWAY_PORT is already in use by another process."
    ss -ltnp | grep ":$GATEWAY_PORT" || true
    exit 1
  fi
  echo "▶️  Starting Ask gateway on :$GATEWAY_PORT ..."
  echo "    ASK_API_URL=$ASK_API_URL  FORWARD_TIMEOUT_S=$FORWARD_TIMEOUT_S"

  nohup $CMD_BASE >>"$LOG_FILE" 2>&1 & echo $! > "$PID_FILE"
  sleep 1
  if is_running; then
    echo "✅ Started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"
  else
    echo "❌ Failed to start. See $LOG_FILE"
    exit 1
  fi
}

stop() {
  if ! is_running; then
    echo "ℹ️ Not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid; pid="$(cat "$PID_FILE")"
  echo "⏹  Stopping pid $pid ..."
  kill "$pid" 2>/dev/null || true
  for _ in {1..25}; do is_running || break; sleep 0.2; done
  if is_running; then
    echo "⚠️  Force killing pid $pid"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "✅ Stopped."
}

restart() { stop; start; }

status() {
  if is_running; then
    echo "✅ Running (pid $(cat "$PID_FILE")) on http://$GATEWAY_HOST:$GATEWAY_PORT"
  else
    echo "❌ Not running."
    if ss -ltnp | grep -q ":$GATEWAY_PORT"; then
      echo "   Note: another process is using :$GATEWAY_PORT"
      ss -ltnp | grep ":$GATEWAY_PORT" || true
    fi
  fi
}

logs() {
  [[ -f "$LOG_FILE" ]] || touch "$LOG_FILE"
  echo "📜 Tailing $LOG_FILE  (Ctrl+C to exit)"
  tail -f "$LOG_FILE"
}

health() {
  curl -s "http://127.0.0.1:$GATEWAY_PORT/twilio/health" || true
  echo
}

case "${1:-}" in
  start)   start   ;;
  stop)    stop    ;;
  restart) restart ;;
  status)  status  ;;
  logs)    logs    ;;
  health)  health  ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs|health}"
    exit 2
    ;;
esac
