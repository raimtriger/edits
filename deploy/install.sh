#!/usr/bin/env bash
# Установка LUDUS EDITS на сервер.
#   git clone https://github.com/raimtriger/edits /opt/edits && bash /opt/edits/deploy/install.sh <BOT_TOKEN>
# Ничего существующего не трогает: nginx, рилсы, боты остаются как были.
# Отдельный сервис edits на порту 8444 с сертификатом api.duddl.ru.
set -euo pipefail

APP=/opt/edits
PORT=8444
DOMAIN=api.duddl.ru
TOKEN="${1:-}"

echo "== проверки"
[ -d "$APP/server" ] || { echo "Нет $APP/server — сначала git clone"; exit 1; }
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] || { echo "Нет сертификата $DOMAIN"; exit 1; }
if ss -ltn | grep -q ":$PORT "; then
  if ! systemctl is-active --quiet edits; then echo "Порт $PORT занят другим процессом — стоп"; exit 1; fi
fi
command -v ffmpeg >/dev/null || { echo "Нет ffmpeg"; exit 1; }

echo "== пакеты"
apt-get install -y python3-venv >/dev/null
apt-get install -y fonts-inter >/dev/null 2>&1 || echo "  fonts-inter нет в репозитории — будет запасной шрифт"
fc-cache -f >/dev/null 2>&1 || true

echo "== python-окружение"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/server/requirements.txt"

echo "== папки"
mkdir -p "$APP/data" "$APP/footage"/{night,love,nature,bokeh,cinema,stock}
# ссылки на уже скачанные стоки рилсов (только чтение, ничего не копирует и не удаляет)
if [ -z "$(ls -A "$APP/footage/stock")" ]; then
  find /opt/moneart /opt/raim -type f \( -iname '*.mp4' -o -iname '*.mov' \) \
       -not -path '*/out/*' -not -path '*/public/*' -not -path '*/tracks/*' 2>/dev/null \
    | shuf -n 80 | while read -r f; do ln -sf "$f" "$APP/footage/stock/$(basename "$(dirname "$f")")_$(basename "$f")"; done || true
fi
echo "  футажей в stock: $(ls "$APP/footage/stock" | wc -l)"

echo "== настройки"
MEM_GB=$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)
MODEL=small; [ "$MEM_GB" -lt 3 ] && MODEL=base
CPUS=$(nproc)
if [ ! -f "$APP/.env" ]; then
  cat > "$APP/.env" <<EOF
BOT_TOKEN=$TOKEN
WEBAPP_URL=https://$DOMAIN:$PORT/
OWNER_IDS=
FREE_EDITS=3
WHISPER_MODEL=$MODEL
WHISPER_THREADS=$(( CPUS > 1 ? CPUS - 1 : 1 ))
EDITS_NICE=15
EOF
  chmod 600 "$APP/.env"
elif [ -n "$TOKEN" ]; then
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$TOKEN|" "$APP/.env"
fi
echo "  RAM ${MEM_GB} ГБ, ядер $CPUS, модель распознавания: $(grep WHISPER_MODEL "$APP/.env" | cut -d= -f2)"

echo "== сервис"
cp "$APP/deploy/edits.service" /etc/systemd/system/edits.service
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl restart edits\n' > /etc/letsencrypt/renewal-hooks/deploy/edits-restart.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/edits-restart.sh
systemctl daemon-reload
systemctl enable edits >/dev/null 2>&1
systemctl restart edits

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow $PORT/tcp >/dev/null && echo "  ufw: открыт порт $PORT"
fi

sleep 4
if curl -sk "https://127.0.0.1:$PORT/api/health" | grep -q ok; then
  echo "== ГОТОВО: https://$DOMAIN:$PORT/"
else
  echo "== Сервис не отвечает. Лог:"; journalctl -u edits -n 30 --no-pager
fi
