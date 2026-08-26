#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh" >&2
  exit 1
fi

APP_USER="${DOWNLOAD_CENTRAL_USER:-pi}"
APP_GROUP="${DOWNLOAD_CENTRAL_GROUP:-pi}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/download-central
STATE_DIR=/var/lib/download-central
ENV_FILE=/etc/download-central.env

if ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "Service user ${APP_USER} does not exist. Set DOWNLOAD_CENTRAL_USER first." >&2
  exit 1
fi

apt-get update
apt-get install --yes openssl python3 python3-venv

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${INSTALL_DIR}" "${STATE_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${INSTALL_DIR}/templates"
install -m 0644 "${SOURCE_DIR}/app.py" "${SOURCE_DIR}/wsgi.py" "${SOURCE_DIR}/requirements.txt" "${INSTALL_DIR}/"
install -m 0644 "${SOURCE_DIR}/templates/index.html" "${INSTALL_DIR}/templates/index.html"

for engine in /opt/raspi-download-manager/app.py /opt/hls-video-downloader/app.py /opt/pi-ytdlp-web/app.py; do
  if [[ ! -f "${engine}" ]]; then
    echo "Required downloader engine not found: ${engine}" >&2
    exit 1
  fi
done

if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --requirement "${INSTALL_DIR}/requirements.txt"
chown -R "${APP_USER}:${APP_GROUP}" "${INSTALL_DIR}" "${STATE_DIR}"

install -m 0755 "${SOURCE_DIR}/download-central-admin" /usr/local/sbin/download-central-admin
install -m 0644 "${SOURCE_DIR}/download-central.service" /etc/systemd/system/download-central.service
sed -i "s/^User=pi$/User=${APP_USER}/; s/^Group=pi$/Group=${APP_GROUP}/" /etc/systemd/system/download-central.service

cat > /etc/sudoers.d/download-central <<EOF
${APP_USER} ALL=(root) NOPASSWD: /usr/local/sbin/download-central-admin apply-settings, /usr/local/sbin/download-central-admin update-ffmpeg
EOF
chmod 0440 /etc/sudoers.d/download-central
visudo -cf /etc/sudoers.d/download-central

if [[ ! -f "${ENV_FILE}" ]]; then
  ADMIN_TOKEN="$(openssl rand -hex 24)"
  cat > "${ENV_FILE}" <<EOF
ADMIN_TOKEN=${ADMIN_TOKEN}
EOF
  chmod 0640 "${ENV_FILE}"
  chown root:"${APP_GROUP}" "${ENV_FILE}"
else
  ADMIN_TOKEN="$(sed -n 's/^ADMIN_TOKEN=//p' "${ENV_FILE}" | head -n 1)"
fi

LEGACY_UNITS=(raspi-download-manager.service hls-video-downloader.service ytdlp-web.service)
systemctl disable --now "${LEGACY_UNITS[@]}" 2>/dev/null || true
for unit in "${LEGACY_UNITS[@]}"; do
  rm -f "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable download-central.service
systemctl restart download-central.service

echo
echo "Download Central is available at http://192.168.1.5:100"
echo "Admin token: ${ADMIN_TOKEN}"
echo "Save this token; it is required for settings and tool updates."
echo "The three legacy downloader service units were stopped, disabled, and removed."
