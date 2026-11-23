"""Превью-кадр для готового mp4: из СЕРЕДИНЫ ролика, а не первый (первый часто тёмный из-за появления на старте).

make_thumb(path, jpg=None) — сохраняет кадр в jpg и возвращает путь (или None при ошибке).
Нужен там, где превью задаётся отдельно от видео, например thumbnail в Telegram.
В сам mp4 кадр НЕ вшивается: вторая видеодорожка ломает загрузку в Instagram/TikTok.
"""
from __future__ import annotations

import os
import subprocess


def _duration(path: str) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip().split()[0])
    except Exception:
        return 0.0


def make_thumb(path: str, jpg: str | None = None, at: float = 0.5, width: int = 320) -> str | None:
    jpg = jpg or os.path.splitext(path)[0] + "_poster.jpg"
    d = _duration(path)
    t = max(0.0, d * at) if d > 0 else 1.0
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "3", jpg], check=True, capture_output=True, timeout=60)
        return jpg if os.path.exists(jpg) and os.path.getsize(jpg) > 500 else None
    except Exception:
        return None
