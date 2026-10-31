#!/usr/bin/env bash
# Шаг Б: аппка и VPN вместе на 443.
#   nginx слушает 443 и смотрит на имя (SNI), TLS НЕ расшифровывает:
#     app.ludusedits.ru  -> аппка (127.0.0.1:8444)
#     всё остальное      -> VPN xray (127.0.0.1:4443), клиентам VPN ничего менять не надо
# Перерыв VPN — несколько секунд. Запасной вход VPN на 9443 не трогается.
# Откат: bash /opt/edits/deploy/router-off.sh
set -euo pipefail
DOMAIN=${1:-app.ludusedits.ru}
DB=/etc/x-ui/x-ui.db
STAMP=$(date +%Y%m%d-%H%M%S)

echo "== проверки"
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] || { echo "Нет сертификата $DOMAIN — сначала domain-cert.sh"; exit 1; }
[ -f /etc/nginx/modules-enabled/50-mod-stream.conf ] || { echo "Нет модуля stream в nginx"; exit 1; }
VID=$(python3 -c "
import sqlite3;c=sqlite3.connect('$DB')
r=c.execute(\"select id from inbounds where port=443 and protocol='vless'\").fetchone()
print(r[0] if r else '')")
[ -n "$VID" ] || { echo "Не нашёл вход VPN на 443 (может, уже перенесён?)"; exit 1; }
echo "  вход VPN id=$VID на 443"
ss -ltn | grep -q ':4443 ' && { echo "Порт 4443 уже кем-то занят — стоп, ничего не менял"; exit 1; }

echo "== бэкапы"
cp "$DB" "/root/x-ui.db.bak-$STAMP"
cp /etc/nginx/nginx.conf "/root/nginx.conf.bak-$STAMP"
cp /opt/edits/.env "/root/edits.env.bak-$STAMP"
echo "$STAMP" > /root/ludus-router.stamp
echo "$VID" > /root/ludus-router.vid

echo "== nginx stream (роутер по имени)"
mkdir -p /etc/nginx/stream.d
cat > /etc/nginx/stream.d/ludus.conf <<EOF
stream {
    log_format ludus '\$time_local \$remote_addr sni=\$ssl_preread_server_name -> \$upstream_addr '
                     'status=\$status sent=\$bytes_sent recv=\$bytes_received time=\$session_time';
    access_log /var/log/nginx/stream443.log ludus;
    map \$ssl_preread_server_name \$ludus_backend {
        $DOMAIN  127.0.0.1:8444;
        default  127.0.0.1:4443;
    }
    server {
        listen 443;
        listen [::]:443;
        ssl_preread on;
        proxy_pass \$ludus_backend;
        proxy_connect_timeout 5s;
        proxy_timeout 1h;
    }
}
EOF
grep -q 'stream.d/\*.conf' /etc/nginx/nginx.conf || printf '\ninclude /etc/nginx/stream.d/*.conf;\n' >> /etc/nginx/nginx.conf
if ! nginx -t 2>/tmp/nginx-t.log; then
  cat /tmp/nginx-t.log
  cp "/root/nginx.conf.bak-$STAMP" /etc/nginx/nginx.conf; rm -f /etc/nginx/stream.d/ludus.conf
  echo "nginx -t не прошёл — всё вернул как было"; exit 1
fi

echo "== VPN: 443 -> 127.0.0.1:4443"
systemctl stop x-ui
python3 -c "
import sqlite3;c=sqlite3.connect('$DB')
c.execute(\"update inbounds set port=4443, listen='127.0.0.1' where id=$VID\");c.commit()"
systemctl start x-ui
for i in $(seq 1 15); do ss -ltn | grep -q '127.0.0.1:4443 ' && break; sleep 1; done
ss -ltn | grep -q '127.0.0.1:4443 ' || { echo "VPN не поднялся на 4443 — откатываю"; bash /opt/edits/deploy/router-off.sh; exit 1; }
ss -ltnp | grep ':443 ' | grep -q xray && { echo "xray всё ещё держит 443 — откатываю"; bash /opt/edits/deploy/router-off.sh; exit 1; }

echo "== nginx берёт 443"
systemctl reload nginx
sleep 2
ss -ltnp | grep ':443 ' | grep -q nginx || { echo "nginx не занял 443 — откатываю"; bash /opt/edits/deploy/router-off.sh; exit 1; }

echo "== аппка на новом адресе"
sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=https://$DOMAIN/|" /opt/edits/.env
grep -q '^SSL_DOMAIN=' /opt/edits/.env && sed -i "s|^SSL_DOMAIN=.*|SSL_DOMAIN=$DOMAIN|" /opt/edits/.env || echo "SSL_DOMAIN=$DOMAIN" >> /opt/edits/.env
cp /opt/edits/deploy/edits.service /etc/systemd/system/edits.service
systemctl daemon-reload && systemctl restart edits
for i in $(seq 1 40); do
  curl -s --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/health" | grep -q ok && break; sleep 2
done
if curl -s --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/api/health" | grep -q ok; then
  echo "== ГОТОВО: https://$DOMAIN/ работает, VPN — через 443 как раньше"
else
  echo "Аппка не ответила по новому адресу. VPN при этом работает. Лог:"; journalctl -u edits -n 20 --no-pager
fi
