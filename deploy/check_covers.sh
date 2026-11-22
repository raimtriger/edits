#!/usr/bin/env bash
# Проверка рендера «Сияния» и «Монеты» на сервере. Запуск: bash deploy/check_covers.sh
set -e
cd "$(dirname "$0")/.."
ffmpeg -v error -y -f lavfi -i "color=c=0x8a4b6b:s=800x800" -frames:v 1 /tmp/c.png
ffmpeg -v error -y -f lavfi -i "sine=frequency=110:duration=8" /tmp/a.mp3
for s in glow coin; do
  echo "== $s"
  ARGS=$(venv/bin/python - "$s" <<'PY'
import json, sys
s = sys.argv[1]
print(json.dumps({"style": s, "format": "9x16",
                  "fields": {"caption": "ПРЕМЬЕРА", "title": "Тест", "subtitle": "", "artist": "ЛЮДУС"},
                  "cover": "/tmp/c.png", "audio": "/tmp/a.mp3", "out": f"/tmp/t_{s}.mp4",
                  "start": 0, "dur": 8, "font_path": None, "lines": []}, ensure_ascii=False))
PY
)
  ( time venv/bin/python -m server.engine.job cover "$ARGS" ) 2>&1 | grep -E "ok|real|rror" || true
done
ls -la /tmp/t_glow.mp4 /tmp/t_coin.mp4
echo "== Pages (число > 0 = новый фронт уже на месте):"
curl -s https://raimtriger.github.io/edits/ | grep -c "tplBlock" || true
