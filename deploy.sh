#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@192.168.1.5}"
REMOTE_DIR="${REMOTE_DIR:-/tmp/download-central-deploy}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! "${PI_HOST}" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]]; then
  echo "Error: PI_HOST must use the user@host format."
  exit 1
fi

if [[ ! "${REMOTE_DIR}" =~ ^/(tmp|var/tmp)/[A-Za-z0-9._/-]+$ || "${REMOTE_DIR}" == *".."* ]]; then
  echo "Error: REMOTE_DIR must be a dedicated directory under /tmp or /var/tmp."
  exit 1
fi

for command_name in rsync ssh; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Error: ${command_name} is required on this computer."
    exit 1
  fi
done

echo "Deploying Download Central to ${PI_HOST}..."

rsync -az --delete \
  --exclude=.git \
  --exclude=.idea \
  --exclude=.vscode \
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude=.DS_Store \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  "${SOURCE_DIR}/" "${PI_HOST}:${REMOTE_DIR}/"

# Allocate a terminal so sudo can request a password when the Pi is not
# configured for passwordless sudo.
ssh -t "${PI_HOST}" \
  "cd '${REMOTE_DIR}' && chmod +x install.sh download-central-admin && sudo ./install.sh"

echo "Deployment complete: http://192.168.1.5:100"
