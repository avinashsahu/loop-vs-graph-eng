#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE=".app_scheduler.pid"
OLLAMA_PID_FILE=".ollama.pid"
SCHEDULER_LOG="${APP_SCHEDULER_LOG_PATH:-cron.log}"
OLLAMA_LOG="${APP_OLLAMA_LOG_PATH:-ollama.log}"

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
  status      Show dependency, scheduler, and last-job state
  logs        Follow scheduler/application logs
  run-once    Start dependencies and run all jobs currently due once
  foreground  Start dependencies and keep the scheduler in this terminal
  down        Stop the scheduler and services started by this application
EOF
}

command="${1:-start}"
case "$command" in
    start)
        prepare_dependencies
        start_scheduler
        ;;
    stop)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        ;;
    restart)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
        prepare_dependencies
        start_scheduler
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
    foreground)
        prepare_dependencies
        exec uv run app_scheduler.py
        ;;
    down)
        stop_pid_group "$PID_FILE" "scheduler" "app_scheduler.py"
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
