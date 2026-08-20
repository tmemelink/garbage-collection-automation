#!/usr/bin/env bash
#
# Creates the container this job is meant to live in.
#
# Runs on the *Proxmox host*, not inside a container: it is one `pct create`
# with the smallest settings the job actually needs, so the recommendation does
# not have to be remembered by hand. install.sh then fills the container.
#
#   ./create-container.sh --vmid 120              create it and start it
#   ./create-container.sh --vmid 120 --install    ... and install the app in it
#   ./create-container.sh --vmid 120 --dry-run    print the commands, run nothing
#
# Every default is overridable; the defaults *are* the recommendation, and the
# README's container table is the same numbers written out.

set -euo pipefail

APP_NAME="garbage-collection-automation"

VMID=""
CT_HOSTNAME="gca"

# One HTTP request a day: a second core would only ever idle, and a run peaks
# around 40 MB of memory. The 256 MB is headroom for the one heavy moment -
# install.sh resolving the Python environment - not for the job.
CORES=1
MEMORY=256
SWAP=256

# GiB. A finished install is roughly 0.8 GB: the Debian rootfs, uv, the managed
# 3.14 interpreter and the venv. The rest is room for apt and the logs.
DISK=2

STORAGE="local-lvm"           # where the rootfs lands
TEMPLATE_STORAGE="local"      # where Proxmox keeps its templates
TEMPLATE=""                   # a volid; resolved from the host when empty
TEMPLATE_NAME="debian-12-standard"
BRIDGE="vmbr0"
TIMEZONE="host"               # so the job log reads in the same time as the host
ONBOOT=1
UNPRIVILEGED=1
SSH_KEY=""

START=1
DO_INSTALL=0
DRY_RUN=0

REPO="${REPO:-tmemelink/garbage-collection-automation}"
REF="${REF:-main}"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==> error:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: create-container.sh --vmid <id> [options]     (run on the Proxmox host)

  --vmid <id>          Container id to create (required)
  --hostname <name>    Container hostname (default: gca)
  --cores <n>          CPU cores (default: 1)
  --memory <MiB>       Memory (default: 256)
  --swap <MiB>         Swap (default: 256)
  --disk <GiB>         Root disk (default: 2)
  --storage <name>     Storage for the root disk (default: local-lvm)
  --template <volid>   Template to use (default: newest debian-12-standard)
  --template-storage <name>
                       Storage holding templates (default: local)
  --bridge <name>      Network bridge (default: vmbr0)
  --timezone <tz>      Container time zone, or "host" (default: host)
  --ssh-key <file>     Public key to authorise for root in the container
  --no-start           Create the container but leave it stopped
  --no-onboot          Do not start the container when the host boots
  --install            Run install.sh inside the container afterwards
  --dry-run            Print what would run, touch nothing
  -h, --help           Show this help

Environment: REPO and REF choose what --install downloads, and GITHUB_TOKEN
gets it into a private one - it is handed to the container through a file rather
than a command line, and the container deletes it as it reads it.
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --vmid)             VMID="${2:?--vmid needs a value}"; shift 2 ;;
            --hostname)         CT_HOSTNAME="${2:?--hostname needs a value}"; shift 2 ;;
            --cores)            CORES="${2:?--cores needs a value}"; shift 2 ;;
            --memory)           MEMORY="${2:?--memory needs a value}"; shift 2 ;;
            --swap)             SWAP="${2:?--swap needs a value}"; shift 2 ;;
            --disk)             DISK="${2:?--disk needs a value}"; shift 2 ;;
            --storage)          STORAGE="${2:?--storage needs a value}"; shift 2 ;;
            --template)         TEMPLATE="${2:?--template needs a value}"; shift 2 ;;
            --template-storage) TEMPLATE_STORAGE="${2:?--template-storage needs a value}"; shift 2 ;;
            --bridge)           BRIDGE="${2:?--bridge needs a value}"; shift 2 ;;
            --timezone)         TIMEZONE="${2:?--timezone needs a value}"; shift 2 ;;
            --ssh-key)          SSH_KEY="${2:?--ssh-key needs a value}"; shift 2 ;;
            --no-start)         START=0; shift ;;
            --no-onboot)        ONBOOT=0; shift ;;
            --install)          DO_INSTALL=1; shift ;;
            --dry-run)          DRY_RUN=1; shift ;;
            -h|--help)          usage; exit 0 ;;
            *)                  die "unknown option: $1 (try --help)" ;;
        esac
    done

    [ -n "$VMID" ] || die "--vmid is required (try --help)"
    [ -z "$SSH_KEY" ] || [ -f "$SSH_KEY" ] || die "ssh key file not found: ${SSH_KEY}"
    if [ "$DO_INSTALL" -eq 1 ] && [ "$START" -eq 0 ]; then
        die "--install needs the container running; drop --no-start"
    fi
    # Creating a container is the one thing this script cannot half-do, so the
    # host is checked before anything is built up.
    if [ "$DRY_RUN" -eq 0 ] && ! command -v pct >/dev/null 2>&1; then
        die "pct not found - run this on the Proxmox host, or use --dry-run"
    fi
}

# Prints a command the way it would be typed, then runs it unless this is a dry
# run. A printed line has to survive being pasted back into a shell, so an
# argument is quoted when - and only when - it holds something a shell would read.
run() {
    local arg
    printf '    '
    for arg in "$@"; do
        case "$arg" in
            *[!A-Za-z0-9_@%+=:,./-]*) printf "'%s' " "${arg//\'/\'\\\'\'}" ;;
            *)                        printf '%s ' "$arg" ;;
        esac
    done
    printf '\n'
    [ "$DRY_RUN" -eq 1 ] && return 0
    "$@"
}

# The volid of the template to create from. A host that has never downloaded one
# gets it here rather than failing halfway through a `pct create`.
resolve_template() {
    [ -n "$TEMPLATE" ] && return

    if ! command -v pveam >/dev/null 2>&1; then
        # Dry run on a workstation: show the shape of the volid, since the exact
        # point release is whatever the host would have.
        TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_NAME}_<version>_amd64.tar.zst"
        return
    fi

    TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
        | awk -v name="$TEMPLATE_NAME" '$1 ~ name {print $1}' | sort | tail -n1)"
    [ -n "$TEMPLATE" ] && return

    local available
    pveam update >/dev/null 2>&1 || true
    available="$(pveam available --section system 2>/dev/null \
        | awk -v name="$TEMPLATE_NAME" '$2 ~ name {print $2}' | sort | tail -n1)"
    [ -n "$available" ] || die "no ${TEMPLATE_NAME} template available; pass --template <volid>"

    log "downloading template ${available}"
    if [ "$DRY_RUN" -eq 0 ]; then
        pveam download "$TEMPLATE_STORAGE" "$available" >/dev/null \
            || die "could not download ${available} to ${TEMPLATE_STORAGE}"
    fi
    TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${available}"
}

create_container() {
    log "creating container ${VMID} (${CORES} vCPU, ${MEMORY} MiB, ${DISK} GiB)"

    # No --features: nesting, fuse and mount widen an unprivileged container and
    # the job needs none of them. No --mp: everything it writes fits in the root
    # disk. What is not asked for here is as much of the recommendation as what is.
    local -a cmd=(
        pct create "$VMID" "$TEMPLATE"
        --hostname "$CT_HOSTNAME"
        --ostype debian
        --unprivileged "$UNPRIVILEGED"
        --cores "$CORES"
        --memory "$MEMORY"
        --swap "$SWAP"
        --rootfs "${STORAGE}:${DISK}"
        --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp"
        --timezone "$TIMEZONE"
        --onboot "$ONBOOT"
    )
    [ -n "$SSH_KEY" ] && cmd+=(--ssh-public-keys "$SSH_KEY")

    run "${cmd[@]}"
}

start_container() {
    [ "$START" -eq 1 ] || return 0
    log "starting container ${VMID}"
    run pct start "$VMID"

    [ "$DRY_RUN" -eq 1 ] && return 0
    # The installer's first act is an apt-get update, so give DHCP and the
    # resolver the few seconds they need before handing over.
    local waited=0
    until pct exec "$VMID" -- getent hosts deb.debian.org >/dev/null 2>&1; do
        [ "$waited" -ge 60 ] && die "container ${VMID} has no working network after ${waited}s"
        sleep 2
        waited=$((waited + 2))
    done
}

install_app() {
    [ "$DO_INSTALL" -eq 1 ] || return 0
    log "installing ${APP_NAME} in container ${VMID}"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        install_from_private_repo
        return
    fi
    run pct exec "$VMID" -- bash -c \
        "curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | bash"
}

# A private repository answers both halves of that - the installer and the source
# it downloads - with a 404 unless the request carries a token. The token travels
# in a file rather than in the command: `pct exec`'s arguments are readable in the
# host's process list and in the container's, and a --dry-run prints them.
install_from_private_repo() {
    local remote="/root/.${APP_NAME}-token" carrier
    carrier="$(mktemp)"
    chmod 600 "$carrier"
    [ "$DRY_RUN" -eq 1 ] || printf '%s' "$GITHUB_TOKEN" > "$carrier"
    run pct push "$VMID" "$carrier" "$remote" --perms 600
    rm -f "$carrier"
    run pct exec "$VMID" -- bash -c \
        "export GITHUB_TOKEN=\$(cat ${remote}); rm -f ${remote}; \
curl -fsSL -H \"Authorization: Bearer \${GITHUB_TOKEN}\" \
https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | bash"
}

summary() {
    [ "$DRY_RUN" -eq 1 ] && { echo; log "dry run - nothing was created"; return; }

    cat <<SUMMARY

$(log "container ${VMID} is ready")

  Enter it       pct enter ${VMID}
  Settings       pct config ${VMID}
SUMMARY
    if [ "$DO_INSTALL" -eq 1 ]; then
        cat <<SUMMARY
  Config         pct exec ${VMID} -- editor /etc/${APP_NAME}/config.toml
  Secrets        pct exec ${VMID} -- editor /etc/${APP_NAME}/env
  Run it once    pct exec ${VMID} -- /root/garbage-collection/run-garbage-collection.sh

SUMMARY
    else
        cat <<SUMMARY
  Install        pct exec ${VMID} -- bash -c 'curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | bash'
                 (a private repository needs credentials; see the README)

SUMMARY
    fi
}

main() {
    parse_args "$@"
    resolve_template
    create_container
    start_container
    install_app
    summary
}

main "$@"
