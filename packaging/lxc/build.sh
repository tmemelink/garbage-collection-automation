#!/usr/bin/env bash
#
# Builds a self-contained install bundle for an existing Debian 12 LXC container.
#
# The normal install path is to curl install.sh straight from GitHub. This bundle
# is for containers without internet access to GitHub: copy it in, extract, and
# run ./install.sh --source . instead.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="${ROOT}/dist"

VERSION="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh)["project"]["version"])
PY
)"

NAME="garbage-collection-automation-${VERSION}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> staging ${NAME}"
mkdir -p "${STAGE}/${NAME}"
cp -a \
    "${ROOT}/install.sh" \
    "${ROOT}/pyproject.toml" \
    "${ROOT}/.python-version" \
    "${ROOT}/README.md" \
    "${ROOT}/LICENSE" \
    "${STAGE}/${NAME}/"
cp -a "${ROOT}/src" "${ROOT}/scheduling" "${ROOT}/config" "${ROOT}/ui" "${STAGE}/${NAME}/"

# The page the web interface serves travels; the design sources behind it do not.
rm -rf "${STAGE}/${NAME}/ui/mockups"

# Ship the resolved lockfile when there is one so the container installs the exact
# same dependency versions that were tested here.
if [ -f "${ROOT}/uv.lock" ]; then
    cp -a "${ROOT}/uv.lock" "${STAGE}/${NAME}/"
else
    echo "==> warning: no uv.lock; the container will resolve dependencies itself" >&2
fi

find "${STAGE}/${NAME}" -name '__pycache__' -type d -prune -exec rm -rf {} +

# A checkout may hold a local config or env file with a real token; the installer
# writes its own of each, so the bundle must never carry either.
rm -f "${STAGE}/${NAME}/config/config.toml" "${STAGE}/${NAME}/config/env"

mkdir -p "$DIST"
TARBALL="${DIST}/${NAME}-lxc.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$NAME"

echo "==> built ${TARBALL#"${ROOT}"/}"
cat <<USAGE

Install it in the container with:

    pct push <vmid> ${TARBALL#"${ROOT}"/} /tmp/${NAME}-lxc.tar.gz
    pct exec <vmid> -- bash -c 'cd /tmp && tar -xzf ${NAME}-lxc.tar.gz && ${NAME}/install.sh --source ${NAME}'
USAGE
