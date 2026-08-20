#!/usr/bin/env bash
#
# Installer for garbage-collection-automation.
#
# Runs *inside* an existing Debian 12 LXC container (e.g. one created from the
# Proxmox "debian-12-standard" template). It does not create the container.
#
#   curl -fsSL https://raw.githubusercontent.com/tmemelink/garbage-collection-automation/main/install.sh | bash
#
# While the repository is private that URL answers 404 to anyone without
# credentials, and so does the download the installer does next. Fetch this file
# over ssh (see the README) and run it with --ssh, or set GITHUB_TOKEN.
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
SSH_URL="${SSH_URL:-git@github.com:${REPO}.git}"
FETCH_SSH=0                   # 1 with --ssh: fetch over ssh instead of https
# ssh must not stop to ask anything: an install reached through a pipe or
# `pct exec` has no terminal to answer on, and a prompt there is a hang.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}"
INSTALL_SCHEDULE=1
INSTALL_WEB=1
RUN_NOW=""                    # "" ask when there is a terminal, 1 always, 0 never
CONFIG_WAS_NEW=0

# The values a first install cannot guess: whose address to look up, and the key
# the schedule API expects. Empty means unanswered - --no-prompt and an install
# with no terminal leave the example values in config.toml, and the first run is
# then the thing that says which one it stopped on.
POSTCODE=""
HOUSE_NUMBER=""
ADDITION=""
API_KEY=""
ASK_CONFIG=1

# The by-hand command lands under root's home rather than under $HOME: this
# installer is usually reached through `pct exec` or a pipe, neither of which
# reliably sets HOME, and root's is the shell you land in with `pct enter`. It
# gets a folder of its own there instead of sitting loose next to the dotfiles,
# so `ls ~` says what this container is for.
HOME_BASE_DIR="${HOME_BASE_DIR:-$(getent passwd root 2>/dev/null | cut -d: -f6)}"
HOME_BASE_DIR="${HOME_BASE_DIR:-/root}"
HOME_CMD_DIR="${HOME_CMD_DIR:-${HOME_BASE_DIR}/garbage-collection}"
HOME_CMD="${HOME_CMD:-${HOME_CMD_DIR}/run-garbage-collection.sh}"
HOME_WEB_CMD="${HOME_WEB_CMD:-${HOME_CMD_DIR}/run-web-interface.sh}"

# uv keeps its managed Python here rather than under /root, so the unprivileged
# service user can actually execute the interpreter the venv points at.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/var/cache/uv}"
UV_BIN=/usr/local/bin/uv
APT_LISTS_DIR="${APT_LISTS_DIR:-/var/lib/apt/lists}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
CRON_DIR="${CRON_DIR:-/etc/cron.d}"

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
  --ssh               Fetch the source over ssh (git@github.com) rather than
                      https, for a private repository this host holds a key
                      for. Without it an https download that fails falls back
                      to ssh anyway when there is a key to try.
  --no-schedule       Install the application but do not add the cron entry.
  --no-web            Install the application but not the web interface service.
                      The port and whether it listens are [web] in config.toml.
  --postcode <pc>     The address to look up, e.g. 1234AB.
  --house-number <n>  Its house number, digits only.
  --addition <a>      Its letter or suffix, when it has one.
  --api-key <key>     The mijnafvalwijzer.nl app key ([collection] api_key).
                      A first install asks for these four when they are not
                      given and there is a terminal to ask on; an upgrade never
                      asks, because the config file it keeps holds the answers.
  --no-prompt         Do not ask for any of them; write the example values.
  --run-now           Run the job once when the install finishes, without asking.
  --no-run-now        Do not run it, and do not ask. Without either flag the
                      installer asks, and skips the question when there is no
                      terminal to ask on.
  --uninstall         Remove the application, user and schedule. Keeps config.
  -h, --help          Show this help.

Environment overrides: APP_USER, INSTALL_DIR, CONFIG_DIR, STATE_DIR, LOG_DIR,
HOME_CMD_DIR (the folder the by-hand commands go in, default
~/garbage-collection), HOME_CMD, HOME_WEB_CMD, REPO, SSH_URL, GITHUB_TOKEN
(either of the last two gets into a private repository).
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)         REF="${2:?--ref needs a value}"; shift 2 ;;
            --source)      SOURCE="${2:?--source needs a value}"; shift 2 ;;
            --ssh)         FETCH_SSH=1; shift ;;
            --no-schedule) INSTALL_SCHEDULE=0; shift ;;
            --no-web)      INSTALL_WEB=0; shift ;;
            --postcode)    POSTCODE="$(squeeze "${2:?--postcode needs a value}")"
                           valid_postcode "$POSTCODE" \
                               || die "--postcode '${2}' is not a Dutch postcode, e.g. 1234AB"
                           shift 2 ;;
            --house-number)
                           HOUSE_NUMBER="$(squeeze "${2:?--house-number needs a value}")"
                           valid_house_number "$HOUSE_NUMBER" \
                               || die "--house-number '${2}' must be digits only; a letter is --addition"
                           shift 2 ;;
            --addition)    ADDITION="$(squeeze "${2:?--addition needs a value}")"
                           valid_addition "$ADDITION" \
                               || die "--addition '${2}' must be letters and digits, e.g. A"
                           shift 2 ;;
            --api-key)     API_KEY="$(squeeze "${2:?--api-key needs a value}")"
                           valid_api_key "$API_KEY" \
                               || die "--api-key is not a value config.toml can hold"
                           shift 2 ;;
            --no-prompt)   ASK_CONFIG=0; shift ;;
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

# Whether a cron daemon is already up. This reads /proc rather than calling pgrep
# or pidof, neither of which a --no-install-recommends container is guaranteed to
# have, and it deliberately does not trust /run/crond.pid: a cron whose pidfile
# went missing is still a cron, and not starting a second one next to it is the
# whole point of asking.
cron_is_running() {
    local dir comm
    for dir in /proc/[0-9]*; do
        [ -r "${dir}/comm" ] || continue
        read -r comm < "${dir}/comm" 2>/dev/null || continue
        [ "$comm" = "cron" ] && return 0
    done
    return 1
}

# Whether there is anyone to ask. Piping the installer to bash leaves stdin on
# the script itself, so a question has to go to the terminal directly - and a
# container filled by `pct exec`, cloud-init or CI has no terminal at all, where
# every question this installer asks answers itself instead of hanging.
have_terminal() { ( exec 3</dev/tty ) 2>/dev/null; }

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

apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq --no-install-recommends "$@" >/dev/null
}

install_prereqs() {
    log "installing system packages"
    apt-get update -qq
    # tzdata: due times are Europe/Amsterdam, and zoneinfo reads the system database.
    apt_install ca-certificates curl tar cron tzdata
}

# git is worth carrying only on a host that fetches its source over ssh, so it is
# installed where that turns out to be the way in rather than with the rest.
ensure_git() {
    command -v git >/dev/null 2>&1 && return 0
    log "installing git for the ssh fetch"
    apt_install git openssh-client
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
        mkdir -p "${TMPDIR_CLEANUP}/tarball"
        tar -xzf "$SOURCE" -C "${TMPDIR_CLEANUP}/tarball"
        SRC_DIR="$(extracted_tree "${TMPDIR_CLEANUP}/tarball")"
    else
        download_source
    fi

    [ -n "$SRC_DIR" ] && [ -f "${SRC_DIR}/pyproject.toml" ] \
        || die "extracted source does not look like the project (no pyproject.toml)"
}

# A tarball holds its tree in one directory named after the ref; which name that
# is, is the archive's business rather than ours.
extracted_tree() { find "$1" -mindepth 1 -maxdepth 1 -type d | head -n1; }

# GitHub answers an unauthenticated request for a private repository with a 404 -
# the same answer a ref that does not exist gets - so a failed download cannot say
# which of the two it was. It can only say what the ways in are.
download_source() {
    if [ "$FETCH_SSH" = 1 ]; then
        fetch_over_ssh \
            || die "ssh fetch failed - is ${SSH_URL} readable with the key this host holds?" \
                   "What it is stuck on: git ls-remote ${SSH_URL}"
        return
    fi

    download_over_https && return

    # Falling back is only worth it where there is a key to fall back on: without
    # one this was a public repository, and the ref is the thing that was wrong.
    if has_ssh_key; then
        warn "https download failed; trying ssh, this host has a key"
        fetch_over_ssh && return
    fi
    die "download failed - either ref '${REF}' does not exist," \
        "or the repository is private and this host gave no credentials:" \
        "set GITHUB_TOKEN, or pass --ssh with a key GitHub knows, or --source a local copy."
}

download_over_https() {
    log "downloading ${REPO}@${REF}"
    local url="https://codeload.github.com/${REPO}/tar.gz/${REF}"
    local -a auth=()
    [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
    curl -fsSL "${auth[@]}" "$url" -o "${TMPDIR_CLEANUP}/src.tar.gz" || return 1
    mkdir -p "${TMPDIR_CLEANUP}/download"
    tar -xzf "${TMPDIR_CLEANUP}/src.tar.gz" -C "${TMPDIR_CLEANUP}/download"
    SRC_DIR="$(extracted_tree "${TMPDIR_CLEANUP}/download")"
}

# The other way into a private repository: a key GitHub knows, rather than a
# token. One shallow fetch of the one ref is all that is wanted, and `git archive`
# hands over the tree without the .git directory that would otherwise be installed.
fetch_over_ssh() {
    ensure_git || return 1
    export HOME="${HOME:-$HOME_BASE_DIR}"   # ssh reads the key from ~/.ssh
    log "fetching ${SSH_URL}@${REF} over ssh"
    local work="${TMPDIR_CLEANUP}/git" tree="${TMPDIR_CLEANUP}/ssh/${APP_NAME}"
    mkdir -p "$work" "$tree"
    git -C "$work" init -q || return 1
    git -C "$work" fetch -q --depth=1 "$SSH_URL" "$REF" || return 1
    git -C "$work" archive FETCH_HEAD | tar -x -C "$tree" || return 1
    SRC_DIR="$tree"
}

# Whether this host has an ssh identity at all: an agent, or a key of root's.
has_ssh_key() {
    [ -n "${SSH_AUTH_SOCK:-}" ] && return 0
    compgen -G "${HOME:-$HOME_BASE_DIR}/.ssh/id_*" >/dev/null
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
    # points at, but it must not be able to replace the code it executes. An
    # upgrade runs as root and is the only writer the application tree needs.
    chown -R root:root "$INSTALL_DIR"
    chmod -R u=rwX,go=rX "$INSTALL_DIR"
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

# What the four answers have to look like for the application to accept them:
# the same rules configuration.py enforces, so a typo is caught here rather than
# by the first run a week later. An empty answer never reaches these - skipping
# is a choice, and the example value in the file says so.
valid_postcode()     { [[ "$1" =~ ^[1-9][0-9]{3}[A-Za-z]{2}$ ]]; }
valid_house_number() { [[ "$1" =~ ^[0-9]+$ ]]; }
valid_addition()     { [[ "$1" =~ ^[A-Za-z0-9]{1,10}$ ]]; }
# Not this project's value to define, so the check is only what the file needs:
# one printable token with nothing in it a TOML basic string would have to escape.
valid_api_key()      { [[ "$1" =~ ^[[:print:]]+$ ]] && [[ "$1" != *[\"\\]* ]]; }

# Whitespace is what a paste brings along, never part of a postcode, a house
# number or a key. A postcode typed as "1234 AB" is the ordinary case.
squeeze() { local value="$1"; printf '%s' "${value//[[:space:]]/}"; }

# A sed replacement has two characters of its own: the delimiter below, and & for
# whatever was matched. What an API key may hold is not this project's to decide,
# so both are escaped here rather than forbidden above.
sed_escape() { local value="${1//&/\\&}"; printf '%s' "${value//|/\\|}"; }

# Asks for one answer until it is one the application would accept, and leaves it
# in the variable named by $1. An answer already given on the command line is
# kept; an empty one ends the asking, because a value left out is a value the
# admin means to fill in later.
ask_field() {
    local name="$1" label="$2" example="$3" check="$4" hint="$5"
    local prompt="    ${label}: " reply=""

    [ -n "${!name}" ] && return 0
    [ -n "$example" ] && prompt="    ${label} [${example}]: "

    while :; do
        printf '%s' "$prompt" > /dev/tty
        read -r reply < /dev/tty || { reply=""; break; }
        reply="$(squeeze "$reply")"
        [ -z "$reply" ] && break
        "$check" "$reply" && break
        printf '    \033[1;33m%s\033[0m\n' "$hint" > /dev/tty
        reply=""
    done

    printf -v "$name" '%s' "$reply"
}

# The address to look up and the key the schedule API expects: the two things
# the installer cannot derive and the first run cannot do without. Asked at the
# start, so the minutes of apt and uv that follow are unattended ones - and only
# when there is something to ask (a config file already there belongs to the
# admin, answers included) and somewhere to ask it.
ask_config() {
    [ "$ASK_CONFIG" -eq 0 ] && return 0
    [ -f "${CONFIG_DIR}/config.toml" ] && return 0
    have_terminal || return 0

    printf '\n\033[1;32m==>\033[0m Configuration. Press enter to leave one out; %s\n    then keeps the example value, and the first run says which.\n\n' \
        "${CONFIG_DIR}/config.toml" > /dev/tty

    ask_field POSTCODE     "Postcode"            "1234AB" valid_postcode \
        "four digits and two letters, e.g. 1234AB"
    ask_field HOUSE_NUMBER "House number"        "56"     valid_house_number \
        "digits only - a letter or suffix goes in the addition below"
    ask_field ADDITION     "Addition, if any"    ""       valid_addition \
        "letters and digits, e.g. A"
    ask_field API_KEY      "Afvalwijzer API key" ""       valid_api_key \
        "one token, no spaces - the README says where to read it off"
    printf '\n' > /dev/tty
}

# The example file *is* the template: the answers replace four of its values and
# every comment around them survives, which is most of what makes the installed
# file worth reading. Each of the four keys appears in it exactly once and at the
# start of a line, so a targeted substitution needs no TOML parser to be safe.
render_config() {
    local template="$1" target="$2"
    # A no-op script first, so an install that answered nothing is still a copy
    # rather than a sed with no script at all.
    local -a edits=(-e '')

    [ -n "$POSTCODE" ] \
        && edits+=(-e "s|^postcode = .*|postcode = \"$(sed_escape "${POSTCODE^^}")\"|")
    [ -n "$HOUSE_NUMBER" ] \
        && edits+=(-e "s|^house_number = .*|house_number = \"$(sed_escape "$HOUSE_NUMBER")\"|")
    [ -n "$ADDITION" ] \
        && edits+=(-e "s|^addition = .*|addition = \"$(sed_escape "$ADDITION")\"|")
    [ -n "$API_KEY" ] \
        && edits+=(-e "s|^api_key = .*|api_key = \"$(sed_escape "$API_KEY")\"|")

    sed "${edits[@]}" "$template" > "$target"
}

install_config() {
    # Everything here is root's and stays root's: the directory, the config file
    # and the env file next to it. That is the layout an administrator's editor
    # expects, and it is what keeps the service user out of the directory that
    # holds the token - it may write the one file it is given and create nothing.
    #
    # That one file is config.toml, which the web interface rewrites in place.
    # Its group is the service user's, and whether the group may write it is the
    # only thing the interface costs. Re-running the installer with or without
    # --no-web moves that bit either way.
    local config_mode=0640
    [ "$INSTALL_WEB" -eq 1 ] && config_mode=0660

    install -d -o root -g "$APP_USER" -m 0750 "$CONFIG_DIR"
    if [ -f "${CONFIG_DIR}/config.toml" ]; then
        log "keeping existing ${CONFIG_DIR}/config.toml"
        # The contents are the admin's; who may write them is this install's
        # decision, and an upgrade that added or dropped the interface has to
        # say so here.
        chown "root:${APP_USER}" "${CONFIG_DIR}/config.toml"
        chmod "$config_mode" "${CONFIG_DIR}/config.toml"
    else
        log "writing ${CONFIG_DIR}/config.toml"
        # Rendered next door first: mktemp gives it mode 0600, and it holds the
        # API key for the moment between being written and being installed. It
        # goes in the directory the EXIT trap clears, so an install that fails
        # between the two does not leave the key in /tmp.
        local rendered
        rendered="$(mktemp "${TMPDIR_CLEANUP:-${TMPDIR:-/tmp}}/config.XXXXXX")"
        render_config "${SRC_DIR}/config/config.example.toml" "$rendered"
        install -o root -g "$APP_USER" -m "$config_mode" "$rendered" "${CONFIG_DIR}/config.toml"
        rm -f "$rendered"
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
    fi

    # Re-applied on every install rather than only when the file is written: an
    # install that was interrupted, or one from a version that placed these
    # differently, is then repaired by running the installer again.
    chown "root:${APP_USER}" "${CONFIG_DIR}/env"
    chmod 0640 "${CONFIG_DIR}/env"
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
        "${SRC_DIR}/scheduling/${APP_NAME}.cron" > "${CRON_DIR}/${APP_NAME}"
    # cron ignores files in /etc/cron.d that are executable or group/world writable.
    chown root:root "${CRON_DIR}/${APP_NAME}"
    chmod 0644 "${CRON_DIR}/${APP_NAME}"

    # The file above is live within the minute on its own: cron re-reads
    # /etc/cron.d on every tick, so an entry dropped there needs no restart, no
    # reload and no signal. What the daemon does need is to be running, and to
    # still be running after a reboot - and starting it is the part that must not
    # be done blindly. Told to start next to a cron the init system does not know
    # about, both systemd and the init script exec a second daemon, and that one
    # dies on the lock the first still holds:
    #
    #   cron: can't lock /var/run/crond.pid, otherpid may be 87: Resource temporarily unavailable
    #
    # So a running daemon is left strictly alone, and only enabling - which starts
    # nothing - is unconditional.
    if has_systemd; then
        systemctl enable cron >/dev/null 2>&1 || warn "could not enable the cron service"
    fi

    cron_is_running && return 0

    if has_systemd; then
        systemctl start cron >/dev/null 2>&1 || warn "could not start the cron service"
    else
        service cron start >/dev/null 2>&1 || warn "could not start the cron service"
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

# How to type a command's path: `~/...` when it really is under root's home, and
# the path in full when an override put it somewhere else. Defaults to the job's
# command, which was the only one when this was written.
home_cmd_display() {
    local path="${1:-$HOME_CMD}"
    # SC2088: the ~ below is text in a comment for someone to read and type, not
    # a path for this shell to expand.
    # shellcheck disable=SC2088
    case "$path" in
        "${HOME_BASE_DIR}"/*) printf '~/%s' "${path#"${HOME_BASE_DIR}"/}" ;;
        *)                    printf '%s' "$path" ;;
    esac
}

# The job is meant to be forgotten about, but the first thing anyone does after
# an install is run it once and watch. `pct enter` lands in root's home, so a
# folder of this application's own goes there, with that command in it.
install_home_command() {
    log "writing the by-hand command to ${HOME_CMD}"
    install -d -m 0700 "$(dirname "$HOME_CMD")"

    # SC2094: the display path below only reads the name, never the file being written.
    # shellcheck disable=SC2094
    cat > "$HOME_CMD" <<HEADER
#!/usr/bin/env bash
#
# Run ${APP_NAME} once, right now, with the output on this terminal.
# Cron runs the same wrapper on its own schedule; this is only the by-hand way.
#
#   $(home_cmd_display)             collect, process and export
#   $(home_cmd_display) --dry-run   collect and process, write nothing
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

    # An install from before the folder existed left the command loose in the
    # home directory. Nothing rewrites that copy any more, so it is the one that
    # goes stale: now that the folder holds a fresh one, take the old one away.
    local legacy
    legacy="${HOME_BASE_DIR}/$(basename "$HOME_CMD")"
    if [ "$legacy" != "$HOME_CMD" ] && [ -f "$legacy" ]; then
        log "removing the command the install before this one left in ${HOME_BASE_DIR}"
        rm -f "$legacy"
    fi
}

# The interface's own by-hand command, next to the job's. The service is the copy
# that comes back after a reboot; this one puts the same server on a terminal,
# which is what you want while you are watching the page answer - every request
# is logged here instead of into the journal.
install_home_web_command() {
    if [ "$INSTALL_WEB" -eq 0 ]; then
        # An install that gives the interface up takes its command with it, the
        # same way it takes the unit.
        rm -f "$HOME_WEB_CMD"
        return
    fi

    log "writing the by-hand web command to ${HOME_WEB_CMD}"
    install -d -m 0700 "$(dirname "$HOME_WEB_CMD")"

    # SC2094: the display path below only reads the name, never the file being written.
    # shellcheck disable=SC2094
    cat > "$HOME_WEB_CMD" <<HEADER
#!/usr/bin/env bash
#
# Serve the ${APP_NAME} web interface on this terminal, in the
# foreground, until ctrl-c stops it. Nothing keeps running afterwards.
#
#   $(home_cmd_display "$HOME_WEB_CMD")
#
# ${APP_NAME}-web.service is the same server, started at boot
# and logging to the journal. Only one of the two can hold the port, so this
# one stops rather than starts while the service has it.
#
# Whether the server listens at all is [web] enabled in
# ${CONFIG_DIR}/config.toml: with that false it says so and exits straight
# away, which is the switch working rather than this command failing.
#
# Anything you pass is handed to the server. Written by install.sh, which writes
# it again on every upgrade, so edits here do not survive one.

set -euo pipefail

APP_USER="${APP_USER}"
UNIT="${APP_NAME}-web.service"
WEB_BIN="${INSTALL_DIR}/.venv/bin/${APP_NAME}-web"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
ENV_FILE="${CONFIG_DIR}/env"
STATE_FILE="${STATE_DIR}/state.json"
UI_DIR="${INSTALL_DIR}/ui"
HEADER

    cat >> "$HOME_WEB_CMD" <<'SCRIPT'

[ -x "$WEB_BIN" ] || {
    echo "not installed: ${WEB_BIN}" >&2
    exit 1
}

# Two servers and one port: the one systemd started is already on it, and all
# this one would manage to say is "something is already listening there". Name
# the thing that has it, and what to type to get it back.
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$UNIT"; then
    echo "${UNIT} is already serving the page." >&2
    echo "to have it on this terminal instead: systemctl stop ${UNIT}" >&2
    exit 1
fi

# The three paths the unit passes for the same reason it does: the defaults are
# derived from where the package was imported from, and this run has to read and
# write the very files the scheduled one does.
SERVER=("$WEB_BIN" --config "$CONFIG_FILE" --state "$STATE_FILE" --ui-dir "$UI_DIR" "$@")

# The page's buttons run the pipeline and rewrite config.toml, and both of those
# files belong to the service user. A run as root would leave root-owned files
# behind for the next cron run - which is not root - to fail on.
if [ "$(id -un)" = "$APP_USER" ]; then
    if [ -r "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    fi
    exec "${SERVER[@]}"
fi

# The env file holds the tokens, so it is read after the switch to the service
# user rather than before it: sudo drops what it was not told to keep, and
# telling it would mean putting a token in an argument list that `ps` shows to
# everyone. The file is group-readable by that user, which is all this needs.
SOURCE_THEN_RUN='if [ -r "$1" ]; then set -a; . "$1"; set +a; fi; shift; exec "$@"'

if command -v runuser >/dev/null 2>&1; then
    exec runuser -u "$APP_USER" -- bash -c "$SOURCE_THEN_RUN" web "$ENV_FILE" "${SERVER[@]}"
elif command -v sudo >/dev/null 2>&1; then
    exec sudo -u "$APP_USER" -- bash -c "$SOURCE_THEN_RUN" web "$ENV_FILE" "${SERVER[@]}"
fi

echo "cannot run as ${APP_USER}: neither runuser nor sudo is installed" >&2
exit 1
SCRIPT

    chown root:root "$HOME_WEB_CMD"
    chmod 0755 "$HOME_WEB_CMD"
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

# The last thing that can still be wrong is the thing nobody checks: whether root
# can open the files this installer just wrote. Storage that squashes root, a
# stray immutable bit and a read-only mount all end the same way - an editor that
# will not save - and none of them show up in the mode line, so the kernel's own
# words for it are what gets printed here.
verify_config_is_editable() {
    local file error
    for file in "${CONFIG_DIR}/config.toml" "${CONFIG_DIR}/env"; do
        [ -f "$file" ] || continue
        # Appending nothing opens the file for writing without changing a byte.
        error="$( { : >> "$file"; } 2>&1 )" && continue
        warn "root cannot write ${file}: ${error##*: }"
        warn "$(stat -c '%n is %A %U:%G' "$CONFIG_DIR" "$file" 2>&1 | tr '\n' ';')"
    done
}

# Without a terminal to ask on the answer is no rather than a hang; see
# have_terminal above for why stdin is not the thing that gets asked.
ask_run_now() {
    have_terminal || return 1

    if [ "$CONFIG_WAS_NEW" -eq 1 ] && { [ -z "$POSTCODE" ] || [ -z "$API_KEY" ]; }; then
        printf '\n    %s is still missing the address or the key,\n    so a run now stops at the lookup - but it does prove the install.\n' \
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
    rm -f "${CRON_DIR}/${APP_NAME}" "/etc/logrotate.d/${APP_NAME}" "$HOME_CMD" "$HOME_WEB_CMD"
    # The folder around the commands goes too, but only if it is a folder this
    # installer made: an override that put a command straight into a home
    # directory must not take the home directory with it, and anything else
    # left in there is someone's own and stays.
    local cmd_dir
    for cmd_dir in "$(dirname "$HOME_CMD")" "$(dirname "$HOME_WEB_CMD")"; do
        [ "$cmd_dir" = "$HOME_BASE_DIR" ] && continue
        rmdir --ignore-fail-on-non-empty "$cmd_dir" 2>/dev/null || true
    done
    # The managed interpreter and the cache are this installer's doing too, and
    # they are the biggest thing it ever put on the disk.
    rm -rf "$INSTALL_DIR" "$UV_PYTHON_INSTALL_DIR" "$UV_CACHE_DIR"
    rmdir --ignore-fail-on-non-empty "$(dirname "$UV_PYTHON_INSTALL_DIR")" 2>/dev/null || true
    id -u "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" 2>/dev/null || true
    log "removed. Config, export state and logs were kept: ${CONFIG_DIR}, ${STATE_DIR}, ${LOG_DIR}"
}

summary() {
    # What to say about the config file depends on how it got here: an upgrade
    # kept one, a first install either wrote the answers down or wrote the
    # example values, and only the last of those is still homework.
    local config_note="<- edit this first"
    if [ "$CONFIG_WAS_NEW" -eq 0 ]; then
        config_note="<- kept from the install before this one"
    elif [ -n "$POSTCODE" ] && [ -n "$API_KEY" ]; then
        config_note="<- holds the address and key you gave"
    fi

    cat <<SUMMARY

$(log "${APP_NAME} installed")

  Config     ${CONFIG_DIR}/config.toml   ${config_note}
  Secrets    ${CONFIG_DIR}/env           <- GCA_TODOIST_TOKEN, GCA_AFVALWIJZER_API_KEY
  Command    ${INSTALL_DIR}/.venv/bin/${APP_NAME}
  Schedule   ${CRON_DIR}/${APP_NAME}
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

  Or serve it on your terminal instead, until ctrl-c - the service has to be
  stopped for that, because the two share a port:
      ${HOME_WEB_CMD}

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
    ask_config
    install_prereqs
    install_uv
    fetch_source
    create_user
    install_app
    install_config
    install_schedule
    install_web_service
    install_home_command
    install_home_web_command
    install_logrotate
    reclaim_space
    verify
    verify_config_is_editable
    summary
    maybe_run_now
}

# Sourcing this file defines the steps without running them, which is how the
# test suite exercises them one at a time.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi
