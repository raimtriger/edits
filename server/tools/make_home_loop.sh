#!/usr/bin/env bash
# Собирает короткий беззвучный цикл для главного экрана аппки из футажей: ~8 с, 480x300, ~300–500 КБ.
# Запуск: bash /opt/edits/server/tools/make_home_loop.sh   (можно повторять — каждый раз новая нарезка)
set -euo pipefail
F="${1:-/opt/edits/footage}"; OUT="${2:-/opt/edits/data/home-loop.mp4}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pick() { find "$1" -type f \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.webm' \) 2>/dev/null | shuf -n 4; }
mapfile -t C < <(pick "$F")
# футажей нет — берём готовые эдиты (они твои, вертикальные)
[ "${#C[@]}" -gt 0 ] || mapfile -t C < <(pick "$(dirname "$OUT")/edits")
[ "${#C[@]}" -gt 0 ] || { echo "нет ни футажей, ни готовых эдитов"; exit 1; }
i=0; : > "$T/list.txt"
for c in "${C[@]}"; do
  i=$((i+1))
  nice -n 15 ffmpeg -v error -y -ss 2 -t 2 -i "$c" -an \
    -vf "scale=480:300:force_original_aspect_ratio=increase,crop=480:300,fps=24,eq=brightness=-0.05" \
    -c:v libx264 -preset veryfast -crf 31 -pix_fmt yuv420p "$T/p$i.mp4"
  echo "file '$T/p$i.mp4'" >> "$T/list.txt"
done
mkdir -p "$(dirname "$OUT")"
ffmpeg -v error -y -f concat -safe 0 -i "$T/list.txt" -c copy -movflags +faststart "$OUT"
ls -l "$OUT"
