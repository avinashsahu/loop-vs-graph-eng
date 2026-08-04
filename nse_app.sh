#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE=".app_scheduler.pid"
OLLAMA_PID_FILE=".ollama.pid"
DASHBOARD_PID_FILE=".dashboard_server.pid"
CONTROL_API_PID_FILE=".control_api.pid"
SCHEDULER_LOG="${APP_SCHEDULER_LOG_PATH:-cron.log}"
OLLAMA_LOG="${APP_OLLAMA_LOG_PATH:-ollama.log}"
DASHBOARD_LOG="${APP_DASHBOARD_LOG_PATH:-dashboard_server.log}"
CONTROL_API_LOG="${APP_CONTROL_API_LOG_PATH:-control_api.log}"

configured_value() {
    local name="$1"
    local fallback="$2"
    local value="${!name:-}"
    if [[ -z "$value" && -f .env ]]; then
        value="$(awk -F= -v key="$name" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' .env)"
    fi
    printf '%s' "${value:-$fallback}"
}

process_is_running() {
    local pid_file="$1"
    local expected_command="${2:-}"
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid="$(<"$pid_file")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -n "$expected_command" ]]; then
        [[ -r "/proc/$pid/cmdline" ]] || return 1
        local command_line
        command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
        [[ "$command_line" == *"$expected_command"* ]] || return 1
    fi
}

ollama_is_ready() {
    curl --silent --fail --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null
}

start_ollama() {
    if ollama_is_ready; then
        return
    fi
    if ! command -v ollama >/dev/null 2>&1; then
        echo "error: Ollama is not running and the ollama command is unavailable" >&2
        return 1
    fi

    echo "Starting Ollama..."
    nohup ollama serve >>"$OLLAMA_LOG" 2>&1 </dev/null &
    printf '%s\n' "$!" >"$OLLAMA_PID_FILE"
    for _ in {1..30}; do
        if ollama_is_ready; then
            break
        fi
        sleep 1
    done
    if ! ollama_is_ready; then
        echo "error: Ollama did not become ready; inspect $OLLAMA_LOG" >&2
        return 1
    fi
}

prepare_dependencies() {
    echo "Starting Aerospike..."
    docker compose up -d aerospike
    start_ollama

    local model
    model="$(configured_value LOCAL_LLM_MODEL phi4:14b-q4_K_M)"
    if ! ollama show "$model" >/dev/null 2>&1; then
        echo "error: Ollama model '$model' is not installed" >&2
        echo "Run: ollama pull $model" >&2
        return 1
    fi
}

start_scheduler() {
    if process_is_running "$PID_FILE" "app_scheduler.py"; then
        echo "Scheduler is already running (PID $(<"$PID_FILE"))."
        return
    fi
    unlink "$PID_FILE" 2>/dev/null || true
    echo "Starting scheduler..."
    nohup setsid uv run app_scheduler.py >>"$SCHEDULER_LOG" 2>&1 </dev/null &
    local pid="$!"
    printf '%s\n' "$pid" >"$PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "error: scheduler exited during startup; inspect $SCHEDULER_LOG" >&2
        return 1
    fi
    echo "NSE Stock Picker is running (PID $pid)."
    echo "Logs: $SCHEDULER_LOG"
}

dashboard_url() {
    local port
    port="$(configured_value APP_DASHBOARD_PORT 8787)"
    printf 'http://127.0.0.1:%s/' "$port"
}

start_dashboard_server() {
    if process_is_running "$DASHBOARD_PID_FILE" "dashboard_server.py"; then
        echo "Dashboard server is already running (PID $(<"$DASHBOARD_PID_FILE"))."
        return
    fi
    unlink "$DASHBOARD_PID_FILE" 2>/dev/null || true
    echo "Starting dashboard server..."
    nohup setsid uv run dashboard_server.py >>"$DASHBOARD_LOG" 2>&1 </dev/null &
    local pid="$!"
    printf '%s\n' "$pid" >"$DASHBOARD_PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "error: dashboard server exited during startup; inspect $DASHBOARD_LOG" >&2
        return 1
    fi
    echo "Dashboard: $(dashboard_url)"
}

control_api_url() {
    local port
    port="$(configured_value APP_CONTROL_API_PORT 8788)"
    printf 'http://127.0.0.1:%s/' "$port"
}

start_control_api() {
    if process_is_running "$CONTROL_API_PID_FILE" "webapp.backend.main:app"; then
        echo "Control API is already running (PID $(<"$CONTROL_API_PID_FILE"))."
        return
    fi
    unlink "$CONTROL_API_PID_FILE" 2>/dev/null || true
    local port
    port="$(configured_value APP_CONTROL_API_PORT 8788)"
    echo "Starting control API..."
    nohup setsid uv run uvicorn webapp.backend.main:app \
        --host 127.0.0.1 --port "$port" \
        >>"$CONTROL_API_LOG" 2>&1 </dev/null &
    local pid="$!"
    printf '%s\n' "$pid" >"$CONTROL_API_PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "error: control API exited during startup; inspect $CONTROL_API_LOG" >&2
        return 1
    fi
    echo "Control API: $(control_api_url)"
}

stop_pid_group() {
    local pid_file="$1"
    local label="$2"
    local expected_command="$3"
    if ! process_is_running "$pid_file" "$expected_command"; then
        unlink "$pid_file" 2>/dev/null || true
        echo "$label is not running."
        return
    fi

    local pid
    pid="$(<"$pid_file")"
    echo "Stopping $label (PID $pid)..."
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..15}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            unlink "$pid_file" 2>/dev/null || true
            return
        fi
        sleep 1
    done
    echo "warning: $label is still stopping; its current job may need to exit" >&2
}

show_status() {
    if process_is_running "$PID_FILE" "app_scheduler.py"; then
        echo "Scheduler: running (PID $(<"$PID_FILE"))"
    else
        echo "Scheduler: stopped"
    fi
    if process_is_running "$DASHBOARD_PID_FILE" "dashboard_server.py"; then
        echo "Dashboard: running (PID $(<"$DASHBOARD_PID_FILE")) at $(dashboard_url)"
    else
        echo "Dashboard: stopped"
    fi
    if process_is_running "$CONTROL_API_PID_FILE" "webapp.backend.main:app"; then
        echo "Control API: running (PID $(<"$CONTROL_API_PID_FILE")) at $(control_api_url)"
    else
        echo "Control API: stopped"
    fi
    if ollama_is_ready; then
        echo "Ollama: ready"
    else
        echo "Ollama: unavailable"
    fi
    docker compose ps aerospike
    if [[ -f .app_scheduler_state.json ]]; then
        echo
        uv run app_scheduler.py --show-state
    fi
}

usage() {
    cat <<'EOF'
Usage: ./nse_app.sh COMMAND

Commands:
  start       Start Aerospike, Ollama if needed, and the background scheduler
  stop        Stop the scheduler
  restart     Restart the scheduler and verify dependencies
  status      Show dependency, scheduler, dashboard, and last-job state
  dashboard   Ensure the dashboard server is running and open it in a browser
  logs        Follow scheduler/application logs
  run-once    Start dependencies and run all jobs currently due once
  foreground  Start dependencies and keep the scheduler in this terminal
  down        Stop the scheduler, dashboard, and services started by this application
EOF
}

command="${1:-start}"
case "$command" in
    start)
        prepare_dependencies
        start_scheduler
        start_dashboard_server
        start_control_api
        ;;
    stop)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        ;;
    restart)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        prepare_dependencies
        start_scheduler
        start_dashboard_server
        start_control_api
        ;;
    status)
        show_status
        ;;
    logs)
        touch "$SCHEDULER_LOG"
        tail -f "$SCHEDULER_LOG"
        ;;
    run-once)
        prepare_dependencies
        exec uv run app_scheduler.py --once
        ;;
    dashboard)
        start_dashboard_server
        command -v xdg-open >/dev/null 2>&1 && xdg-open "$(dashboard_url)" >/dev/null 2>&1 || true
        ;;
    foreground)
        prepare_dependencies
        exec uv run app_scheduler.py
        ;;
    down)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        stop_pid_group "$DASHBOARD_PID_FILE" "dashboard server" "dashboard_server.py"
        stop_pid_group "$CONTROL_API_PID_FILE" "control API" "webapp.backend.main:app"
        docker compose down
        if [[ -f "$OLLAMA_PID_FILE" ]]; then
            stop_pid_group "$OLLAMA_PID_FILE" "managed Ollama" "ollama serve"
        fi
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
