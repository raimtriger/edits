#!/usr/bin/env bash
# Включает журнал подключений на 443 (роутер nginx stream): кто, на какое имя, куда ушло, сколько байт.
# Ничего не меняет в маршрутизации. Если nginx -t не прошёл — возвращает старый файл.
# Смотреть: tail -f /var/log/nginx/stream443.log
set -euo pipefail
CONF=/etc/nginx/stream.d/ludus.conf
[ -f "$CONF" ] || { echo "Нет $CONF — роутер не включён"; exit 1; }
grep -q stream443.log "$CONF" && { echo "Журнал уже включён: tail -f /var/log/nginx/stream443.log"; exit 0; }
cp "$CONF" "$CONF.bak"
python3 - "$CONF" <<'EOF'
import sys
p = sys.argv[1]; h = open(p).read()
h = h.replace("stream {\n", """stream {
    log_format ludus '$time_local $remote_addr sni=$ssl_preread_server_name -> $upstream_addr '
                     'status=$status sent=$bytes_sent recv=$bytes_received time=$session_time';
    access_log /var/log/nginx/stream443.log ludus;
""", 1)
open(p, "w").write(h)
EOF
if nginx -t 2>/tmp/nginx-t.log; then
  systemctl reload nginx && echo "Журнал включён: tail -f /var/log/nginx/stream443.log"
else
  cat /tmp/nginx-t.log; cp "$CONF.bak" "$CONF"; echo "nginx -t не прошёл — вернул как было"; exit 1
fi
