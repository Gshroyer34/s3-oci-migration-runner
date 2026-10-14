#!/usr/bin/env bash
# Bootstrap only the installer toolchain on a fresh Oracle Linux 9 x86_64 host.
# Verify the handoff archive SHA-256 before extracting and running this script.
set -euo pipefail
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
. /etc/os-release
test "$ID" = ol
test "${VERSION_ID%%.*}" = 9
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
test ! -e /opt/migration-installer
test ! -L /opt/migration-installer
test ! -e /etc/migration/image-id
umask 077
# Dependencies come from the configured OS/Python repositories. This requires
# egress; it does not install the runtime image or create any cloud resources.
dnf install -y python3.12 python3.12-pip podman
python3.12 -m venv /opt/migration-installer
/opt/migration-installer/bin/python -m pip install -r "$root/requirements-cloud.lock" -r "$root/requirements-build.lock"
/opt/migration-installer/bin/python -m pip install --no-build-isolation --no-deps "$root"
/opt/migration-installer/bin/python -m migration_runner.installation.host_cli --help
printf '%s\n' 'Installer ready. Follow PORTABLE_INSTALLER.md to install the reviewed runtime release and enroll this node.'
