#!/bin/bash
# Version: 3.1.2
# Path: ~/projects/documind-hr/documind_hr.sh
# Purpose: Manage DocuMind-HR FastAPI service (start/stop/restart/status/log)

APP_DIR="$HOME/projects/documind-hr"
VENV="$APP_DIR/venv/bin/activate"
ENV_FILE="$APP_DIR/.env.hr"
APP_MODULE="server_hr:app"
HOST="0.0.0.0"
PORT="9001"
LOGFILE="$APP_DIR/logs/documind_hr.log"
PIDFILE="$APP_DIR/documind_hr.pid"

start() {
  if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "Service already running (PID $(cat $PIDFILE))"
    exit 1
  fi
  mkdir -p "$APP_DIR/logs"
  source "$VENV"
  echo "Starting DocuMind-HR on port $PORT ..."
  ENV_FILE="$ENV_FILE" uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" --app-dir "$APP_DIR" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "✅ Started (PID $(cat $PIDFILE))"
}

stop() {
  if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "Stopping service (PID $(cat $PIDFILE))..."
    kill $(cat "$PIDFILE")
    rm -f "$PIDFILE"
    echo "🛑 Stopped."
  else
    echo "Service not running."
  fi
}

restart() {
  stop
  sleep 1
  start
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "✅ Running (PID $(cat $PIDFILE))"
  else
    echo "🛑 Not running"
  fi
}

log() {
  tail -f "$LOGFILE"
}

case "$1" in
  start|stop|restart|status|log) "$1" ;;
  *) echo "Usage: $0 {start|stop|restart|status|log}" ;;
esac
