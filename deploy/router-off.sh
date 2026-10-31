#!/usr/bin/env bash
# Откат роутера: VPN снова сам на 443, nginx отпускает 443, аппка — на старом адресе api.duddl.ru:8444.
set -u
STAMP=$(cat /root/ludus-router.stamp 2>/dev/null || true)
VID=$(cat /root/ludus-router.vid 2>/dev/null || true)
rm -f /etc/nginx/stream.d/ludus.conf
[ -n "$STAMP" ] && [ -f "/root/nginx.conf.bak-$STAMP" ] && cp "/root/nginx.conf.bak-$STAMP" /etc/nginx/nginx.conf
nginx -t && systemctl reload nginx
sleep 1
systemctl stop x-ui
if [ -n "$VID" ]; then
  python3 -c "
import sqlite3;c=sqlite3.connect('/etc/x-ui/x-ui.db')
c.execute(\"update inbounds set port=443, listen='' where id=$VID\");c.commit()"
fi
systemctl enable x-ui >/dev/null 2>&1; systemctl start x-ui
[ -n "$STAMP" ] && [ -f "/root/edits.env.bak-$STAMP" ] && cp "/root/edits.env.bak-$STAMP" /opt/edits/.env
systemctl restart edits
sleep 3
ss -ltnp | grep ':443 '
echo "Откат выполнен: VPN на 443, аппка на api.duddl.ru:8444"
