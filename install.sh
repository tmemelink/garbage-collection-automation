#!/usr/bin/env bash
#
# Installer for garbage-collection-automation.
#
# Runs *inside* an existing Debian 12 LXC container (e.g. one created from the
# Proxmox "debian-12-standard" template). It does not create the container.
#
#   curl -fsSL https://raw.githubusercontent.com/tmemelink/garbage-collection-automation/main/install.sh | bash
#
# Re-running upgrades an existing install in place; the config file is never
# overwritten. Run with --uninstall to remove everything except the config.

set -euo pipefail

APP_NAME="garbage-collection-automation"
APP_USER="${APP_USER:-gca}"
INSTALL_DIR="${INSTALL_DIR:-/opt/${APP_NAME}}"
CONFIG_DIR="${CONFIG_DIR:-/etc/${APP_NAME}}"
STATE_DIR="${STATE_DIR:-/var/lib/${APP_NAME}}"
LOG_DIR="${LOG_DIR:-/var/log/${APP_NAME}}"

REPO="${REPO:-tmemelink/garbage-collection-automation}"
REF="${REF:-main}"
SOURCE="${SOURCE:-}"          # local dir or tarball; skips the download when set
INSTALL_SCHEDULE=1
INSTALL_WEB=1
RUN_NOW=""                    # "" ask when there is a terminal, 1 always, 0 never
CONFIG_WAS_NEW=0

# The by-hand command lands in root's home rather than in $HOME: this installer
# is usually reached through `pct exec` or a pipe, neither of which reliably sets
# HOME, and root's is the shell you land in with `pct enter`.
HOME_CMD_DIR="${HOME_CMD_DIR:-$(getent passwd root 2>/dev/null | cut -d: -f6)}"
HOME_CMD_DIR="${HOME_CMD_DIR:-/root}"
HOME_CMD="${HOME_CMD:-${HOME_CMD_DIR}/run-garbage-collection.sh}"

# uv keeps its managed Python here rather than under /root, so the unprivileged
# service user can actually execute the interpreter the venv points at.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/var/cache/uv}"
UV_BIN=/usr/local/bin/uv
APT_LISTS_DIR="${APT_LISTS_DIR:-/var/lib/apt/lists}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

TMPDIR_CLEANUP=""
trap 'test -n "$TMPDIR_CLEANUP" && rm -rf "$TMPDIR_CLEANUP"' EXIT

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==> error:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: install.sh [options]

  --ref <git-ref>     Branch, tag or commit to install (default: main)
  --source <path>     Install from a local directory or tarball instead of
                      downloading; use this for air-gapped installs.
  --no-schedule       Install the application but do not add the cron entry.
  --no-web            Install the application but not the web interface service.
                      The port and whether it listens are [web] in config.toml.
  --run-now           Run the job once when the install finishes, without asking.
  --no-run-now        Do not run it, and do not ask. Without either flag the
                      installer asks, and skips the question when there is no
                      terminal to ask on.
  --uninstall         Remove the application, user and schedule. Keeps config.
  -h, --help          Show this help.

Environment overrides: APP_USER, INSTALL_DIR, CONFIG_DIR, STATE_DIR, LOG_DIR,
HOME_CMD, REPO, GITHUB_TOKEN (for private repositories).
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)         REF="${2:?--ref needs a value}"; shift 2 ;;
            --source)      SOURCE="${2:?--source needs a value}"; shift 2 ;;
            --no-schedule) INSTALL_SCHEDULE=0; shift ;;
            --no-web)      INSTALL_WEB=0; shift ;;
            --run-now)     RUN_NOW=1; shift ;;
            --no-run-now)  RUN_NOW=0; shift ;;
            --uninstall)   uninstall; exit 0 ;;
            -h|--help)     usage; exit 0 ;;
            *)             die "unknown option: $1 (try --help)" ;;
        esac
    done
}

# Debian 12 has systemd, but the installer must not fall over on a container that
# was built without it: the job itself is cron, and only the web interface is a unit.
has_systemd() { [ -d /run/systemd/system ]; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root inside the container"
}

check_platform() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}" in
            debian|ubuntu) : ;;
            *) warn "expected Debian, found '${PRETTY_NAME:-unknown}' - continuing anyway" ;;
        esac
    fi
    if ! [ -f /run/systemd/container ] && ! grep -qa container=lxc /proc/1/environ 2>/dev/null; then
        warn "this does not look like an LXC container - continuing anyway"
    fi
}

install_prereqs() {
    log "installing system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # tzdata: due times are Europe/Amsterdam, and zoneinfo reads the system database.
    apt-get install -y -qq --no-install-recommends ca-certificates curl tar cron tzdata >/dev/null
}

install_uv() {
    if [ -x "$UV_BIN" ]; then
        log "updating uv"
        "$UV_BIN" self update >/dev/null 2>&1 || true
        return
    fi
    log "installing uv"
    curl -fsSL https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
    [ -x "$UV_BIN" ] || die "uv installation failed"
}

# Populates $SRC_DIR with the project source tree.
fetch_source() {
    TMPDIR_CLEANUP="$(mktemp -d)"

    if [ -n "$SOURCE" ]; then
        if [ -d "$SOURCE" ]; then
            log "installing from local directory ${SOURCE}"
            SRC_DIR="$SOURCE"
            return
        fi
        [ -f "$SOURCE" ] || die "--source '${SOURCE}' is neither a directory nor a file"
        log "installing from local tarball ${SOURCE}"
        tar -xzf "$SOURCE" -C "$TMPDIR_CLEANUP"
    else
        log "downloading ${REPO}@${REF}"
        local url="https://codeload.github.com/${REPO}/tar.gz/${REF}"
        local -a auth=()
        [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
        curl -fsSL "${auth[@]}" "$url" -o "${TMPDIR_CLEANUP}/src.tar.gz" \
            || die "download failed - check that ref '${REF}' exists and the repo is reachable"
        tar -xzf "${TMPDIR_CLEANUP}/src.tar.gz" -C "$TMPDIR_CLEANUP"
    fi

    SRC_DIR="$(find "$TMPDIR_CLEANUP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    [ -n "$SRC_DIR" ] && [ -f "${SRC_DIR}/pyproject.toml" ] \
        || die "extracted source does not look like the project (no pyproject.toml)"
}

create_user() {
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        log "creating service user ${APP_USER}"
        useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
    fi
    install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
}

install_app() {
    log "installing application to ${INSTALL_DIR}"
    install -d -m 0755 "$INSTALL_DIR" "${INSTALL_DIR}/bin"

    # Replace the code but leave .venv/ in place so upgrades are cheap. Everything
    # uv reads has to come from the source, the lockfile included: it is what makes
    # the container install the exact versions the bundle was resolved against.
    rm -rf "${INSTALL_DIR}/src"
    cp -a "${SRC_DIR}/src" "${INSTALL_DIR}/src"
    cp -a "${SRC_DIR}/pyproject.toml" "${INSTALL_DIR}/pyproject.toml"
    # pyproject.toml points license-files at LICENSE, so uv's build backend
    # refuses the project when the file is absent: it is not optional here.
    [ -f "${SRC_DIR}/LICENSE" ] \
        || die "the source has no LICENSE, which pyproject.toml's license-files requires"
    cp -a "${SRC_DIR}/LICENSE" "${INSTALL_DIR}/LICENSE"
    for optional in uv.lock .python-version README.md; do
        if [ -f "${SRC_DIR}/${optional}" ]; then
            cp -a "${SRC_DIR}/${optional}" "${INSTALL_DIR}/${optional}"
        else
            # Never let a previous install's copy outlive the source it came from.
            rm -f "${INSTALL_DIR}/${optional}"
        fi
    done
    if [ -f "${INSTALL_DIR}/uv.lock" ]; then
        log "installing the dependency versions pinned in uv.lock"
    else
        warn "no uv.lock in the source; dependencies will be resolved fresh"
    fi

    # The web interface is static files the server reads; they live next to the
    # code so that one upgrade replaces both, and never one without the other.
    rm -rf "${INSTALL_DIR}/ui"
    if [ -d "${SRC_DIR}/ui" ]; then
        cp -a "${SRC_DIR}/ui" "${INSTALL_DIR}/ui"
        # Design sources are for the workstation, not for a 2 GiB container.
        rm -rf "${INSTALL_DIR}/ui/mockups"
    else
        warn "no ui/ in the source; the web interface will have nothing to serve"
    fi

    install -m 0755 "${SRC_DIR}/src/run-job.sh" "${INSTALL_DIR}/bin/run-job.sh"

    log "resolving Python environment (this can take a minute on first run)"
    ( cd "$INSTALL_DIR" && "$UV_BIN" sync --no-dev --quiet )

    # The service user has to read the tree and execute the interpreter the venv
    # points at, both of which live outside its home.
    chown -R "${APP_USER}:${APP_USER}" "$INSTALL_DIR"
    chmod -R a+rX "$INSTALL_DIR"
    if [ -d "$UV_PYTHON_INSTALL_DIR" ]; then
        chmod a+rX "$(dirname "$UV_PYTHON_INSTALL_DIR")"
        chmod -R a+rX "$UV_PYTHON_INSTALL_DIR"
    else
        # uv downloads an interpreter only when the host has none that fits. When
        # it reused the host's there is nothing here, and chmod must not be the
        # thing that fails the install.
        log "uv reused an interpreter already on the host"
    fi
}

install_config() {
    # Who owns config.toml is what decides whether the web interface's save
    # button works. A save is a temp file plus a rename, so the service user
    # needs the file and its directory both; without the interface nothing but
    # root ever writes it and it stays root's. Re-running the installer with or
    # without --no-web moves it either way.
    local config_owner=root config_dir_mode=0750
    if [ "$INSTALL_WEB" -eq 1 ]; then
        config_owner="$APP_USER"
        # Group-writable so the rename can land; sticky so that is all it buys.
        # In a sticky directory only the owner of an entry may replace it, which
        # keeps env - root's, and where the token lives - out of the service
        # user's reach even though it may now write next to it.
        config_dir_mode=1770
    fi

    install -d -o root -g "$APP_USER" -m "$config_dir_mode" "$CONFIG_DIR"
    if [ -f "${CONFIG_DIR}/config.toml" ]; then
        log "keeping existing ${CONFIG_DIR}/config.toml"
        # The contents are the admin's; the ownership is this install's decision,
        # and an upgrade that added or dropped the interface has to move it.
        chown "${config_owner}:${APP_USER}" "${CONFIG_DIR}/config.toml"
        chmod 0640 "${CONFIG_DIR}/config.toml"
    else
        log "writing default config to ${CONFIG_DIR}/config.toml"
        install -o "$config_owner" -g "$APP_USER" -m 0640 \
            "${SRC_DIR}/config/config.example.toml" "${CONFIG_DIR}/config.toml"
        # It still holds the example address, which is what makes the run offered
        # at the end of a first install a question worth qualifying.
        CONFIG_WAS_NEW=1
    fi

    # run-job.sh sources this before every run; it is how a secret reaches a cron
    # job, which starts with an almost empty environment.
    if [ -f "${CONFIG_DIR}/env" ]; then
        log "keeping existing ${CONFIG_DIR}/env"
    else
        log "writing ${CONFIG_DIR}/env"
        cat > "${CONFIG_DIR}/env" <<ENVFILE
# Environment for ${APP_NAME}, sourced by run-job.sh before every run.
#
# Secrets belong here rather than in config.toml: this file is never part of an
# install bundle, and an upgrade never overwrites it. One KEY=value per line, no
# quotes needed; the value runs to the end of the line.
#
# The Todoist API token (Todoist > Settings > Integrations > Developer):
#GCA_TODOIST_TOKEN=
#
# The mijnafvalwijzer.nl app key. Set it here or as [collection] api_key in
# config.toml - this file wins when both have one, and the web interface can
# only write the config.toml side. See the README for where to read it off.
#GCA_AFVALWIJZER_API_KEY=
ENVFILE
        chown "root:${APP_USER}" "${CONFIG_DIR}/env"
        chmod 0640 "${CONFIG_DIR}/env"
    fi
}

install_schedule() {
    if [ "$INSTALL_SCHEDULE" -eq 0 ]; then
        log "skipping schedule (--no-schedule)"
        return
    fi
    log "installing cron schedule"
    sed -e "s|@APP_USER@|${APP_USER}|g" \
        -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
        -e "s|@LOG_DIR@|${LOG_DIR}|g" \
        "${SRC_DIR}/scheduling/${APP_NAME}.cron" > "/etc/cron.d/${APP_NAME}"
    # cron ignores files in /etc/cron.d that are executable or group/world writable.
    chown root:root "/etc/cron.d/${APP_NAME}"
    chmod 0644 "/etc/cron.d/${APP_NAME}"

    if has_systemd; then
        systemctl enable --now cron >/dev/null 2>&1 || warn "could not enable the cron service"
    else
        service cron restart >/dev/null 2>&1 || warn "could not restart the cron service"
    fi
}

# The one part of this project that keeps running. It is installed enabled, and
# whether it listens is [web] enabled in config.toml - see the unit's own comment.
install_web_service() {
    local unit="${APP_NAME}-web.service"
    if [ "$INSTALL_WEB" -eq 0 ]; then
        log "skipping the web interface (--no-web)"
        # Also the way an install that had it gives it up again.
        if [ -f "${SYSTEMD_DIR}/${unit}" ]; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
            rm -f "${SYSTEMD_DIR}/${unit}"
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        return
    fi
    if ! has_systemd; then
        warn "no systemd here, so the web interface cannot be installed as a service"
        return
    fi

    log "installing the web interface service"
    sed -e "s|@APP_USER@|${APP_USER}|g" \
        -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
        -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
        -e "s|@STATE_DIR@|${STATE_DIR}|g" \
        "${SRC_DIR}/scheduling/${unit}" > "${SYSTEMD_DIR}/${unit}"
    chown root:root "${SYSTEMD_DIR}/${unit}"
    chmod 0644 "${SYSTEMD_DIR}/${unit}"

    systemctl daemon-reload
    systemctl enable "$unit" >/dev/null 2>&1 || warn "could not enable ${unit}"
    # restart rather than start: this is also the upgrade path, and the running
    # process is holding the code that was just replaced.
    systemctl restart "$unit" || warn "could not start ${unit}; see journalctl -u ${unit}"
}

# The job is meant to be forgotten about, but the first thing anyone does after
# an install is run it once and watch. `pct enter` lands in root's home, so that
# is where the command to do it goes - one name, no paths to remember.
install_home_command() {
    log "writing the by-hand command to ${HOME_CMD}"
    install -d -m 0700 "$(dirname "$HOME_CMD")"

    # SC2094: the basename below only reads the name, never the file being written.
    # shellcheck disable=SC2094
    cat > "$HOME_CMD" <<HEADER
#!/usr/bin/env bash
#
# Run ${APP_NAME} once, right now, with the output on this terminal.
# Cron runs the same wrapper on its own schedule; this is only the by-hand way.
#
#   ~/$(basename "$HOME_CMD")             collect, process and export
#   ~/$(basename "$HOME_CMD") --dry-run   collect and process, write nothing
#
# Anything you pass is handed to the application. Written by install.sh, which
# writes it again on every upgrade, so edits here do not survive one.

set -euo pipefail

APP_USER="${APP_USER}"
RUN_JOB="${INSTALL_DIR}/bin/run-job.sh"
HEADER

    cat >> "$HOME_CMD" <<'SCRIPT'

[ -x "$RUN_JOB" ] || {
    echo "not installed: ${RUN_JOB}" >&2
    exit 1
}

# The lock and the exported-state record belong to the service user. A run as
# root would leave root-owned files behind in its directory, and the next cron
# run - which is not root - would then fail on files it cannot write.
if [ "$(id -un)" = "$APP_USER" ]; then
    exec "$RUN_JOB" "$@"
elif command -v runuser >/dev/null 2>&1; then
    exec runuser -u "$APP_USER" -- "$RUN_JOB" "$@"
elif command -v sudo >/dev/null 2>&1; then
    exec sudo -u "$APP_USER" -- "$RUN_JOB" "$@"
fi

echo "cannot run as ${APP_USER}: neither runuser nor sudo is installed" >&2
exit 1
SCRIPT

    chown root:root "$HOME_CMD"
    chmod 0755 "$HOME_CMD"
}

install_logrotate() {
    [ -d /etc/logrotate.d ] || return 0
    cat > "/etc/logrotate.d/${APP_NAME}" <<LOGROTATE
${LOG_DIR}/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ${APP_USER} ${APP_USER}
}
LOGROTATE
    chmod 0644 "/etc/logrotate.d/${APP_NAME}"
}

# Everything downloaded to build the install is dead weight once it is built, and
# on a 2 GiB container that is a tenth of the disk. The venv holds hardlinks into
# uv's cache, so clearing it frees the duplicates without touching what runs; an
# upgrade downloads again, which is the right trade for a job that runs once a day.
reclaim_space() {
    log "reclaiming build caches"
    "$UV_BIN" cache clean >/dev/null 2>&1 || warn "could not clear the uv cache"
    apt-get clean >/dev/null 2>&1 || true
    rm -rf "${APT_LISTS_DIR:?}"/* 2>/dev/null || true
}

verify() {
    log "verifying installation"
    local entry="${INSTALL_DIR}/.venv/bin/${APP_NAME}"

    # Ask sudo first, because running as the service user is the thing cron will
    # do. Falling back to root on a *failed* sudo run would hide exactly the
    # permission problem this check exists to catch, so only a missing sudo -
    # it is not in the minimal Debian template - is allowed to weaken it.
    if command -v sudo >/dev/null 2>&1; then
        sudo -u "$APP_USER" "$entry" --version >/dev/null 2>&1 \
            || die "the installed entry point does not run as ${APP_USER}"
        return
    fi
    "$entry" --version >/dev/null || die "the installed entry point does not run"
    warn "sudo is not installed, so this only proves root can run it"
}

# Piping the installer to bash leaves stdin on the script itself, so the question
# has to go to the terminal directly. A container filled by `pct exec`, cloud-init
# or CI has no terminal at all, and there the answer is no rather than a hang.
ask_run_now() {
    ( exec 3</dev/tty ) 2>/dev/null || return 1

    if [ "$CONFIG_WAS_NEW" -eq 1 ]; then
        printf '\n    %s still holds the example address,\n    so a run now stops at the lookup - but it does prove the install.\n' \
            "${CONFIG_DIR}/config.toml" > /dev/tty
    fi
    printf '\n\033[1;32m==>\033[0m Run it once now? [y/N] ' > /dev/tty

    local reply=""
    read -r reply < /dev/tty || return 1
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                 return 1 ;;
    esac
}

# Deliberately the last thing the installer does, and deliberately through the
# same command the user will type from now on: the offer and the documentation
# cannot drift apart if the offer is what the documentation describes.
maybe_run_now() {
    [ "$RUN_NOW" = "0" ] && return 0
    if [ "$RUN_NOW" != "1" ]; then
        ask_run_now || { log "not running it now - ${HOME_CMD} does it whenever you want"; return 0; }
    fi

    log "running the job once"
    local status=0
    "$HOME_CMD" || status=$?
    if [ "$status" -eq 0 ]; then
        log "the run finished"
    else
        # The install is done and verified by this point; a run that fails says
        # something about the configuration, not about the installation.
        warn "the run exited ${status} - the install itself is fine; the README lists what each code means"
    fi
}

uninstall() {
    require_root
    log "removing ${APP_NAME}"
    if [ -f "${SYSTEMD_DIR}/${APP_NAME}-web.service" ]; then
        systemctl disable --now "${APP_NAME}-web.service" >/dev/null 2>&1 || true
        rm -f "${SYSTEMD_DIR}/${APP_NAME}-web.service"
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f "/etc/cron.d/${APP_NAME}" "/etc/logrotate.d/${APP_NAME}" "$HOME_CMD"
    # The managed interpreter and the cache are this installer's doing too, and
    # they are the biggest thing it ever put on the disk.
    rm -rf "$INSTALL_DIR" "$UV_PYTHON_INSTALL_DIR" "$UV_CACHE_DIR"
    rmdir --ignore-fail-on-non-empty "$(dirname "$UV_PYTHON_INSTALL_DIR")" 2>/dev/null || true
    id -u "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" 2>/dev/null || true
    log "removed. Config, export state and logs were kept: ${CONFIG_DIR}, ${STATE_DIR}, ${LOG_DIR}"
}

summary() {
    cat <<SUMMARY

$(log "${APP_NAME} installed")

  Config     ${CONFIG_DIR}/config.toml   <- edit this first
  Secrets    ${CONFIG_DIR}/env           <- GCA_TODOIST_TOKEN, GCA_AFVALWIJZER_API_KEY
  Command    ${INSTALL_DIR}/.venv/bin/${APP_NAME}
  Schedule   /etc/cron.d/${APP_NAME}
  Logs       ${LOG_DIR}/

  Run it once by hand, as the job user, with the output on your terminal:
      ${HOME_CMD}
      ${HOME_CMD} --dry-run    # collect and process, write nothing
SUMMARY

    if [ "$INSTALL_WEB" -eq 1 ]; then
        cat <<SUMMARY

  The web interface is installed but switched off. To use it, set [web] enabled
  to true in ${CONFIG_DIR}/config.toml and then:
      systemctl restart ${APP_NAME}-web

  It listens on localhost only. From your workstation:
      ssh -N -L 8080:127.0.0.1:8080 root@<container>   # then open http://127.0.0.1:8080/
SUMMARY
    fi
    echo
}

main() {
    parse_args "$@"
    require_root
    check_platform
    install_prereqs
    install_uv
    fetch_source
    create_user
    install_app
    install_config
    install_schedule
    install_web_service
    install_home_command
    install_logrotate
    reclaim_space
    verify
    summary
    maybe_run_now
}

# Sourcing this file defines the steps without running them, which is how the
# test suite exercises them one at a time.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi
