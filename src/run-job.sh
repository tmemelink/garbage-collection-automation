#!/usr/bin/env bash
#
# Job wrapper. Cron invokes it on the installed container, and it is also the
# way to run the pipeline by hand from a repository checkout.
#
# It keeps the crontab line short, guarantees only one run at a time, and gives
# every line a timestamp so the log is readable.
#
#   installed   <install dir>/bin/run-job.sh   config in /etc, state in /var/lib
#   checkout    <repo>/src/run-job.sh          config, venv, lock and state in the repo
#
# Either layout sources an optional env file next to the config first, which is
# where secrets such as GCA_TODOIST_TOKEN live.
#
# A checkout run never reads or writes anything outside the repository, and it
# never touches cron - use install.sh for that.

set -euo pipefail

APP_NAME="garbage-collection-automation"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

# install.sh copies this script to <install dir>/bin/; in the repository it sits
# in <repo>/src/. That difference is what tells the two layouts apart.
if [ "$(basename "$SCRIPT_DIR")" = "src" ] && [ -f "${ROOT}/pyproject.toml" ]; then
    REPO_MODE=1
    INSTALL_DIR="${INSTALL_DIR:-$ROOT}"
    CONFIG_FILE="${CONFIG_FILE:-${ROOT}/config/config.toml}"
    ENV_FILE="${ENV_FILE:-${ROOT}/config/env}"
    LOCK_FILE="${LOCK_FILE:-${ROOT}/.local/run.lock}"
    STATE_FILE="${STATE_FILE:-${ROOT}/.local/state.json}"
    mkdir -p "$(dirname "$LOCK_FILE")"
else
    REPO_MODE=0
    INSTALL_DIR="${INSTALL_DIR:-/opt/${APP_NAME}}"
    CONFIG_FILE="${CONFIG_FILE:-/etc/${APP_NAME}/config.toml}"
    ENV_FILE="${ENV_FILE:-/etc/${APP_NAME}/env}"
    # Not /tmp: any local user could sit on a lock file there and quietly stop
    # every run. The state directory is the service user's own, mode 0750.
    LOCK_FILE="${LOCK_FILE:-${STATE_DIR:-/var/lib/${APP_NAME}}/run.lock}"
    STATE_FILE="${STATE_FILE:-${STATE_DIR:-/var/lib/${APP_NAME}}/state.json}"
fi

# Secrets belong outside config.toml, and cron inherits nothing from anyone's
# shell - so this file is the only way GCA_TODOIST_TOKEN reaches a scheduled run.
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

BIN="${INSTALL_DIR}/.venv/bin/${APP_NAME}"

if [ -x "$BIN" ]; then
    CMD=("$BIN")
elif [ "$REPO_MODE" -eq 1 ] && command -v uv >/dev/null 2>&1; then
    # No venv yet: uv creates one inside the checkout on first use.
    CMD=(uv run --project "$ROOT" --quiet "$APP_NAME")
elif [ "$REPO_MODE" -eq 1 ]; then
    echo "no environment yet: run 'uv sync' in ${ROOT}" >&2
    exit 1
else
    echo "not installed: ${BIN}" >&2
    exit 1
fi

if [ ! -r "$CONFIG_FILE" ]; then
    echo "missing config: ${CONFIG_FILE}" >&2
    [ "$REPO_MODE" -eq 1 ] && echo "create it with: cp config/config.example.toml config/config.toml" >&2
    exit 1
fi

# flock returns 1 when another run still holds the lock; treat that as success so
# cron does not mail about an overlap.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -Is) another run is still in progress, skipping"
    exit 0
fi

echo "$(date -Is) starting ${APP_NAME}"
status=0
"${CMD[@]}" --config "$CONFIG_FILE" --state "$STATE_FILE" "$@" || status=$?
echo "$(date -Is) finished with exit code ${status}"
exit "$status"
