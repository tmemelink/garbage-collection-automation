#!/usr/bin/env bash
#
# Build wrapper. Dispatches to the per-target build script in packaging/.
#
#   ./build.sh lxc      Build the offline install bundle for an LXC container.
#   ./build.sh --list   Show available targets.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A target is buildable when its directory holds a build script we can run; both
# the listing and the dispatch below ask this same question, so they cannot
# disagree about whether a target exists or is merely planned.
is_implemented() { [ -x "${1}/build.sh" ] && [ -s "${1}/build.sh" ]; }

list_targets() {
    echo "Available targets:"
    for dir in "${ROOT}"/packaging/*/; do
        [ -d "$dir" ] || continue
        target="$(basename "$dir")"
        if is_implemented "${dir%/}"; then
            printf '  %-8s %s\n' "$target" "packaging/${target}/build.sh"
        else
            printf '  %-8s (not implemented yet)\n' "$target"
        fi
    done
}

main() {
    local target="${1:-}"

    case "$target" in
        ""|-h|--help)
            echo "Usage: ./build.sh <target> [options]"
            echo
            list_targets
            exit 0
            ;;
        --list)
            list_targets
            exit 0
            ;;
    esac

    local dir="${ROOT}/packaging/${target}"

    # A directory under packaging/ is a target this project has; whether it holds
    # a runnable script decides between "planned" and "ready", never "unknown".
    if [ ! -d "$dir" ]; then
        echo "error: unknown target '${target}'" >&2
        echo >&2
        list_targets >&2
        exit 1
    fi
    if ! is_implemented "$dir"; then
        echo "error: target '${target}' is not implemented yet (packaging/${target}/)" >&2
        exit 1
    fi

    shift
    exec "${dir}/build.sh" "$@"
}

main "$@"
