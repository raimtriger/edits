#!/usr/bin/env bash
# Полный откат LUDUS EDITS. Остальные сервисы сервера не трогает.
# Данные (треки, эдиты) сохраняются в /opt/edits-backup-<дата>.
set -u
systemctl disable --now edits 2>/dev/null
rm -f /etc/systemd/system/edits.service /etc/letsencrypt/renewal-hooks/deploy/edits-restart.sh
systemctl daemon-reload
command -v ufw >/dev/null && ufw delete allow 8444/tcp 2>/dev/null
[ -d /opt/edits ] && mv /opt/edits "/opt/edits-backup-$(date +%Y%m%d-%H%M)"
echo "LUDUS EDITS удалён, данные в /opt/edits-backup-*"
