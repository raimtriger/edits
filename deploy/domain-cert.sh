#!/usr/bin/env bash
# Шаг А: сертификат для app.ludusedits.ru (через уже работающий nginx на 80 порту).
# Добавляет ОДИН новый файл nginx только для этого адреса. Существующие сайты не трогает.
# Откат: rm /etc/nginx/sites-enabled/ludusedits-app && systemctl reload nginx
set -euo pipefail
DOMAIN=${1:-app.ludusedits.ru}
IP=$(curl -s4 https://api.ipify.org || true)

echo "== DNS"
got=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)
echo "  $DOMAIN -> ${got:-нет}  (сервер: ${IP:-?})"
[ -n "$got" ] || { echo "DNS ещё не обновился — подожди и запусти снова"; exit 1; }

echo "== nginx: блок для $DOMAIN на 80 порту"
mkdir -p /var/www/ludusedits
cat > /etc/nginx/sites-available/ludusedits-app <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/ludusedits; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
ln -sf /etc/nginx/sites-available/ludusedits-app /etc/nginx/sites-enabled/ludusedits-app
if ! nginx -t 2>/tmp/nginx-t.log; then
  cat /tmp/nginx-t.log; rm -f /etc/nginx/sites-enabled/ludusedits-app
  echo "nginx -t не прошёл — файл убран, ничего не изменилось"; exit 1
fi
systemctl reload nginx

echo "== сертификат"
certbot certonly --webroot -w /var/www/ludusedits -d "$DOMAIN" --non-interactive --agree-tos \
  --keep-until-expiring --register-unsafely-without-email
ls -l "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" && echo "== ГОТОВО: сертификат есть"
