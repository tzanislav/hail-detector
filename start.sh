#!/usr/bin/env bash
# Start the dashboard API and its background radar poller from this directory.
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
RUN_DIR="$APP_DIR/run"
ARCHIVE_DIR="$APP_DIR/var/radar"
PORT="${HAIL_PORT:-8001}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Missing virtual environment at $VENV_DIR. Run the deployment setup first." >&2
  exit 1
fi

mkdir -p "$RUN_DIR" "$ARCHIVE_DIR"

for service in api poller; do
  pid_file="$RUN_DIR/$service.pid"
  if [[ -f "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "$service is already running (PID $pid)." >&2
      exit 1
    fi
    rm -f "$pid_file"
  fi
done

export HAIL_ARCHIVE_DIR="$ARCHIVE_DIR"

nohup "$VENV_DIR/bin/python" -m hail_detector.poller >"$RUN_DIR/poller.log" 2>&1 &
poller_pid=$!
echo "$poller_pid" >"$RUN_DIR/poller.pid"

nohup "$VENV_DIR/bin/python" -m uvicorn hail_detector.api:app \
  --app-dir "$APP_DIR/src" --host 0.0.0.0 --port "$PORT" \
  >"$RUN_DIR/api.log" 2>&1 &
api_pid=$!
echo "$api_pid" >"$RUN_DIR/api.pid"

sleep 2
if ! kill -0 "$poller_pid" 2>/dev/null || ! kill -0 "$api_pid" 2>/dev/null; then
  echo "One or more services failed to start. Check $RUN_DIR/*.log" >&2
  exit 1
fi

echo "Hail detector started: http://<EC2-IP>:$PORT/"
echo "API PID: $api_pid; poller PID: $poller_pid"
