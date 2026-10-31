"""Движок монтажа эдитов.

Трек + строки текста с таймингами + папка футажей -> вертикальное видео 1080x1920.
Зависимости: только ffmpeg/ffprobe (с libass) и Python 3.10+.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

W, H, FPS = 1080, 1920, 30
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


@dataclass
class Line:
    start: float  # секунды от начала трека
    end: float
    text: str


@dataclass
class Style:
    mode: str = "line"          # "line" — строкой, "word" — по одному слову
    font: str = "Inter Light"   # имя семейства (fontconfig)
    font_size: int = 72
    bw: bool = False            # чёрно-белый
    cut: float = 1.0            # средняя длина склейки, сек
    darken: float = 0.10        # затемнение под текст (0..0.4)
    show_next: bool = True      # показывать следующую строку блекло (режим line)
    watermark: str | None = None  # надпись внизу (бесплатный тариф)
    fonts_dir: str | None = None  # папка со шрифтами для libass
    seed: int | None = None


@dataclass
class EditJob:
    audio: Path
    lines: list[Line]
    footage: list[Path]
    out: Path
    start: float | None = None  # откуда резать трек; None = с первой строки
    duration: float | None = None
    style: Style = field(default_factory=Style)


# ---------- утилиты ----------

NICE = int(os.environ.get("EDITS_NICE", "0"))  # >0 — рендер с пониженным приоритетом, чтобы не мешать другим сервисам


def _run(cmd: list[str]) -> str:
    if NICE and cmd[0] == "ffmpeg":
        cmd = ["nice", "-n", str(NICE)] + cmd
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{p.stderr[-2000:]}")
    return p.stdout


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


def list_footage(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in VIDEO_EXT)


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


# ---------- субтитры ----------

def build_ass(lines: list[Line], offset: float, length: float, style: Style) -> str:
    """Строки -> ASS-файл. offset — где начинается отрывок в треке."""
    fs = style.font_size
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{style.font},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,0,2,5,120,120,0,1
Style: Next,{style.font},{int(fs*0.8)},&H70FFFFFF,&H70FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,0,1,5,140,140,0,1
Style: Word,{style.font},{int(fs*1.45)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,0,2,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev: list[str] = []
    cx, cy = W // 2, int(H * 0.52)
    visible = [ln for ln in lines if ln.end > offset and ln.start < offset + length]

    for i, ln in enumerate(visible):
        s = max(0.0, ln.start - offset)
        e = min(length, ln.end - offset)
        if e <= s:
            continue
        if style.mode == "word":
            words = ln.text.split()
            if not words:
                continue
            # длительность слова пропорциональна числу букв
            total = sum(len(w) + 2 for w in words)
            t = s
            for w in words:
                d = (e - s) * (len(w) + 2) / total
                ev.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + d)},Word,,0,0,0,,"
                          f"{{\\pos({cx},{cy})\\fad(60,40)}}{_ass_escape(w)}")
                t += d
        else:
            ev.append(f"Dialogue: 1,{_ass_time(s)},{_ass_time(e)},Main,,0,0,0,,"
                      f"{{\\pos({cx},{cy})\\fad(180,150)}}{_ass_escape(ln.text)}")
            if style.show_next and i + 1 < len(visible):
                nxt = visible[i + 1]
                ev.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Next,,0,0,0,,"
                          f"{{\\pos({cx},{cy + int(fs * 1.9)})\\fad(180,150)}}{_ass_escape(nxt.text)}")
    if style.watermark:
        ev.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(length)},Next,,0,0,0,,"
                  f"{{\\pos({cx},{H - 140})\\fs30\\alpha&H60&}}{_ass_escape(style.watermark)}")
    return header + "\n".join(ev) + "\n"


# ---------- нарезка футажа ----------

def plan_cuts(footage: list[Path], length: float, cut: float, rng: random.Random):
    """Возвращает список (файл, offset, dur). Без повторов подряд."""
    durs = {p: probe_duration(p) for p in footage}
    plan, t, last = [], 0.0, None
    while t < length - 0.01:
        d = min(length - t, cut * rng.uniform(0.8, 1.25))
        if length - t - d < 0.4:      # не оставлять огрызок в конце
            d = length - t
        choices = [p for p in footage if p != last and durs[p] > 0.3] or list(footage)
        p = rng.choice(choices)
        max_off = max(0.0, durs[p] - d - 0.05)
        off = rng.uniform(0, max_off) if max_off > 0 else 0.0
        plan.append((p, off, min(d, durs[p])))
        t += d
        last = p
    return plan


# ---------- сборка ----------

def render_edit(job: EditJob) -> Path:
    st = job.style
    rng = random.Random(st.seed)
    if not job.lines:
        raise ValueError("нет строк текста")
    if not job.footage:
        raise ValueError("нет футажей")

    start = job.start if job.start is not None else max(0.0, job.lines[0].start - 0.3)
    if job.duration is not None:
        length = job.duration
    else:
        last_end = max(ln.end for ln in job.lines)
        length = min(45.0, last_end - start + 0.6)
    length = min(length, probe_duration(job.audio) - start)
    if length < 3:
        raise ValueError("отрывок слишком короткий")

    plan = plan_cuts(job.footage, length, st.cut, rng)

    with tempfile.TemporaryDirectory() as tmp:
        ass_path = Path(tmp) / "subs.ass"
        ass_path.write_text(build_ass(job.lines, start, length, st), encoding="utf-8")

        cmd = ["ffmpeg", "-y", "-v", "error"]
        for p, off, d in plan:
            cmd += ["-ss", f"{off:.3f}", "-t", f"{d:.3f}", "-i", str(p)]
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(job.audio)]

        parts = []
        for i, (_, _, d) in enumerate(plan):
            parts.append(
                f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"fps={FPS},setsar=1,format=yuv420p,trim=duration={d:.3f},setpts=PTS-STARTPTS[v{i}]"
            )
        chain = "".join(f"[v{i}]" for i in range(len(plan)))
        look = []
        if st.bw:
            look.append("hue=s=0")
        if st.darken > 0:
            look.append(f"eq=brightness={-st.darken:.2f}:contrast=1.05")
        look.append("vignette=PI/5")
        ass_arg = str(ass_path).replace("\\", "/").replace(":", "\\:")
        if st.fonts_dir:
            fd = str(st.fonts_dir).replace("\\", "/").replace(":", "\\:")
            look.append(f"ass=filename='{ass_arg}':fontsdir='{fd}'")
        else:
            look.append(f"ass='{ass_arg}'")
        parts.append(f"{chain}concat=n={len(plan)}:v=1:a=0,{','.join(look)}[vout]")
        a_idx = len(plan)
        parts.append(f"[{a_idx}:a]afade=t=in:d=0.25,afade=t=out:st={max(0, length-0.6):.3f}:d=0.6[aout]")

        cmd += ["-filter_complex", ";".join(parts),
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-t", f"{length:.3f}",
                str(job.out)]
        job.out.parent.mkdir(parents=True, exist_ok=True)
        _run(cmd)
    return job.out
