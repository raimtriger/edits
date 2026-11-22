#!/usr/bin/env python3
"""Собирает стартовый цикл для первого экрана: ~15 с ярких кадров из роликов, 540x960, без звука.

Запуск (на сервере, под nice, пока лучше ночью):
  nice -n 10 /opt/edits/venv/bin/python /opt/edits/server/tools/make_intro_loop.py [ссылка на папку Яндекс.Диска | папка с видео]
Без аргумента берёт /opt/edits/footage. Ссылка вида https://disk.yandex.ru/d/... — ролики скачиваются во временную папку (до 12 штук, ≤ 250 МБ).
Результат: /opt/edits/data/intro-loop.mp4 (отдаётся на /api/intro-loop.mp4). Прежний файл сохраняется как intro-loop.prev.mp4."""
import json, os, shutil, subprocess, sys, tempfile, urllib.parse, urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("EDITS_ROOT", "/opt/edits"))
OUT = Path(os.environ.get("EDITS_INTRO_OUT", ROOT / "data" / "intro-loop.mp4"))
TOTAL = 15.0
MAX_CLIPS = 11            # сколько нарезок в ролике
PER_VIDEO = 2             # не больше стольких нарезок из одного ролика
EXT = (".mp4", ".mov", ".webm", ".m4v")
API = "https://cloud-api.yandex.net/v1/disk/public/resources"


# ───────── источники ─────────
def yd_items(link, path="/"):
    off = 0
    while True:
        u = f"{API}?{urllib.parse.urlencode({'public_key': link, 'path': path, 'limit': 100, 'offset': off})}"
        with urllib.request.urlopen(u, timeout=30) as r:
            j = json.load(r)
        items = j.get("_embedded", {}).get("items", [])
        for it in items:
            if it["type"] == "dir":
                yield from yd_items(link, it["path"])
            elif it["name"].lower().endswith(EXT) and it.get("size", 0) <= 250 * 2 ** 20:
                yield it
        off += len(items)
        if not items or off >= j.get("_embedded", {}).get("total", 0):
            return


def yd_download(link, limit, dest: Path):
    got = []
    for it in yd_items(link):
        if len(got) >= limit:
            break
        u = f"{API}/download?{urllib.parse.urlencode({'public_key': link, 'path': it['path']})}"
        with urllib.request.urlopen(u, timeout=30) as r:
            href = json.load(r)["href"]
        f = dest / f"{len(got):02d}{Path(it['name']).suffix.lower()}"
        print("скачиваю", it["name"], flush=True)
        with urllib.request.urlopen(href, timeout=120) as r, open(f, "wb") as o:
            shutil.copyfileobj(r, o)
        got.append(f)
    return got


# ───────── выбор ярких кадров ─────────
def probe_dur(f: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def frame_scores(f: Path, fps=2):
    """Оценка каждого кадра (2 в секунду, 36x64): насыщенность + контраст, тёмные и выбеленные штрафуем."""
    w, h = 36, 64
    r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-i", str(f), "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=600)
    a = np.frombuffer(r.stdout, np.uint8)
    n = len(a) // (w * h * 3)
    if n == 0:
        return np.zeros(0)
    a = a[:n * w * h * 3].reshape(n, w * h, 3).astype(np.float32) / 255
    mx, mn = a.max(2), a.min(2)
    sat = ((mx - mn) / (mx + 1e-6)).mean(1)
    luma = a @ np.float32([0.2126, 0.7152, 0.0722])
    lm, ls = luma.mean(1), luma.std(1)
    score = sat * 1.0 + ls * 1.2 + 0.6 * np.clip(1 - np.abs(lm - 0.5) / 0.5, 0, 1)
    score -= 1.0 * (lm < 0.22) + 0.8 * (lm > 0.88)
    return score


def pick(videos, seg):
    cands = []
    k = max(2, int(round(seg * 2)))                     # кадров в окне
    for f in videos:
        sc = frame_scores(f)
        if len(sc) < k:
            continue
        win = np.convolve(sc, np.ones(k) / k, mode="valid")
        order, taken = np.argsort(-win), []
        for i in order:
            if all(abs(i - j) >= k + 2 for j in taken):           # без пересечений и подряд
                taken.append(int(i))
            if len(taken) >= PER_VIDEO:
                break
        for i in taken:
            cands.append((float(win[i]), f, i / 2.0))
    cands.sort(key=lambda x: -x[0])
    return cands[:MAX_CLIPS]


def build(videos, out: Path):
    seg = TOTAL / MAX_CLIPS
    ch = pick(videos, seg)
    if not ch:
        raise SystemExit("нет подходящих роликов")
    seg = TOTAL / len(ch)
    seg = max(1.0, min(2.4, seg))
    # разные ролики подряд, а не два кадра одного — перемешиваем детерминированно
    seen, ordered, rest = set(), [], []
    for c in ch:
        (rest if str(c[1]) in seen else ordered).append(c); seen.add(str(c[1]))
    ch = ordered + rest
    cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-v", "error"]
    flt = []
    for n, (_s, f, ss) in enumerate(ch):
        cmd += ["-ss", f"{ss:.2f}", "-t", f"{seg:.2f}", "-i", str(f)]
        flt.append(f"[{n}:v]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,fps=30,setsar=1,eq=saturation=1.15:contrast=1.04,format=yuv420p[v{n}]")
    flt.append("".join(f"[v{n}]" for n in range(len(ch))) + f"concat=n={len(ch)}:v=1:a=0[v]")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    cmd += ["-filter_complex", ";".join(flt), "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-maxrate", "2500k", "-bufsize", "5M",
            "-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, check=True, timeout=1200)
    if out.exists():
        shutil.copy2(out, out.with_name("intro-loop.prev.mp4"))
    tmp.replace(out)
    print(f"готово: {out} · {len(ch)} нарезок по {seg:.2f} с · {out.stat().st_size / 1e6:.1f} МБ")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "footage")
    if src.startswith("http"):
        with tempfile.TemporaryDirectory() as td:
            vids = yd_download(src, 12, Path(td))
            build(vids, OUT)
    else:
        vids = sorted(p for p in Path(src).rglob("*") if p.suffix.lower() in EXT)
        if len(vids) > 40:                                  # на 1 vCPU не гоняем всё подряд
            import random
            random.Random(1).shuffle(vids)
            vids = vids[:40]
        build(vids, OUT)


if __name__ == "__main__":
    main()
