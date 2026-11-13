"""Делает короткую зацикленную нарезку из футажей для главного экрана аппки (data/loop.mp4, обычно 200–400 КБ).
Запуск на сервере:  /opt/edits/venv/bin/python -m server.tools.make_loop
Футажи берутся из EDITS_FOOTAGE (по умолчанию /opt/edits/footage), 5 случайных клипов по ~0.9 с — как быстрый эдит."""
import os, random, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FOOTAGE = Path(os.environ.get("EDITS_FOOTAGE", ROOT / "footage"))
OUT = Path(os.environ.get("EDITS_DATA", ROOT / "data")) / "loop.mp4"
W, H, N, SEG = 640, 360, 5, 0.9


def dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main():
    clips = [p for p in FOOTAGE.rglob("*") if p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")]
    if not clips:
        sys.exit(f"нет футажей в {FOOTAGE}")
    random.shuffle(clips)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for p in clips:
            if len(parts) >= N:
                break
            d = dur(p)
            if d < SEG + 0.5:
                continue
            out = Path(tmp) / f"s{len(parts)}.mp4"
            ss = random.uniform(0.2, d - SEG - 0.2)
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-t", str(SEG), "-i", str(p),
                                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=24,format=yuv420p",
                                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", str(out)])
            if r.returncode == 0:
                parts.append(out)
        if not parts:
            sys.exit("не удалось нарезать ни одного клипа")
        lst = Path(tmp) / "list.txt"
        lst.write_text("".join(f"file '{x}'\n" for x in parts))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = OUT.with_suffix(".tmp.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-movflags", "+faststart", str(tmp_out)], check=True)
        tmp_out.replace(OUT)
    print(f"готово: {OUT} · {OUT.stat().st_size // 1024} КБ · {len(parts)} клипов")


if __name__ == "__main__":
    main()
