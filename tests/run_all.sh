#!/usr/bin/env bash
# Run the test suites, each with its output in a log file, and summarise.
#
#   tests/run_all.sh              every suite; needs Mathematica
#   tests/run_all.sh --no-kernel  only the suites that run without Mathematica
#
# Uses .venv/bin/python unless PYTHON names another interpreter. Logs go to
# TEST_LOG_DIR, or a new temporary directory.
set -u
cd "$(dirname "$0")/.."

PY=${PYTHON:-.venv/bin/python}
if ! command -v "$PY" > /dev/null; then
    echo "no interpreter at $PY: create .venv (see README, Quick start) or set PYTHON" >&2
    exit 2
fi

unit="test_recorder_foundation test_recorder_core test_recorder_annotations
      test_recorder_finalize test_recorder_adversarial test_recorder_hardening"
live="test_kernel test_recorder_server test_server_mcp test_supervisor"
suites=$unit
[ "${1:-}" = "--no-kernel" ] || suites="$unit $live"

logs=${TEST_LOG_DIR:-$(mktemp -d)}
mkdir -p "$logs"
failed=""
for s in $suites; do
    start=$(date +%s)
    # A file, not a pipe: a pipe holds all output until the suite exits.
    if PYTHONDONTWRITEBYTECODE=1 "$PY" "tests/$s.py" > "$logs/$s.log" 2>&1; then
        status=ok
    else
        status=FAILED
        failed="$failed $s"
    fi
    printf '%-28s %-7s %5ss  %s\n' "$s" "$status" "$(( $(date +%s) - start ))" \
        "$(grep -E 'passed' "$logs/$s.log" | tail -1)"
done
echo "logs: $logs"
if [ -n "$failed" ]; then
    for s in $failed; do
        echo; echo "--- end of $s.log ---"; tail -n 40 "$logs/$s.log"
    done
    exit 1
fi
