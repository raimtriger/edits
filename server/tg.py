"""Telegram: проверка initData мини-аппки, отправка видео, приём /start."""
from __future__ import annotations

import os
import hashlib
import hmac
import json
import logging
import threading
import time
from urllib.parse import parse_qsl

import httpx

log = logging.getLogger("tg")


class Bot:
    def __init__(self, token: str, webapp_url: str):
        self.token = token
        self.webapp_url = webapp_url
        self.base = f"https://api.telegram.org/bot{token}"
        self.on_start = None  # callback(user_dict)

    # ---------- проверка мини-аппки ----------
    def verify_init_data(self, init_data: str, max_age: int = 7 * 86400) -> dict | None:
        """Официальная проверка подписи Telegram WebApp initData."""
        try:
            pairs = dict(parse_qsl(init_data, keep_blank_values=True))
            got = pairs.pop("hash", "")
            check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
            secret = hmac.new(b"WebAppData", self.token.encode(), hashlib.sha256).digest()
            calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(calc, got):
                return None
            if time.time() - int(pairs.get("auth_date", "0")) > max_age:
                return None
            return json.loads(pairs.get("user", "{}"))
        except Exception:
            return None

    # ---------- вызовы API ----------
    def call(self, method: str, **params):
        try:
            r = httpx.post(f"{self.base}/{method}", json=params, timeout=30)
            return r.json()
        except Exception as e:
            log.warning("tg %s failed: %s", method, e)
            return None

    def send_message(self, chat_id: int, text: str, with_app_button: bool = False):
        params = {"chat_id": chat_id, "text": text}
        if with_app_button:
            params["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть LUDUS EDITS", "web_app": {"url": self.webapp_url}}]]}
        return self.call("sendMessage", **params)

    @staticmethod
    def poster_frame(path: str) -> str | None:
        """Кадр-обложка из середины ролика (самый светлый из 5 между 35% и 65%), чтобы превью не было чёрным нулевым кадром."""
        import subprocess, tempfile
        try:
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                       capture_output=True, text=True, timeout=30).stdout.strip())
        except Exception:
            return None
        best, best_l = None, -1.0
        for k in (0.35, 0.42, 0.5, 0.58, 0.65):
            out = tempfile.mktemp(suffix=".jpg", prefix="poster_")
            try:
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * k:.2f}", "-i", path, "-frames:v", "1",
                                    "-vf", "scale=-2:640", "-q:v", "4", out], capture_output=True, timeout=60)
                if r.returncode != 0 or not os.path.exists(out):
                    continue
                m = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-"],
                                   capture_output=True, timeout=30).stdout
                lum = sum(m) / max(1, len(m))
            except Exception:
                continue
            if lum > best_l:
                if best and os.path.exists(best):
                    os.unlink(best)
                best, best_l = out, lum
            else:
                os.unlink(out)
        return best

    def send_video(self, chat_id: int, path: str, caption: str = ""):
        poster = self.poster_frame(path)
        try:
            with open(path, "rb") as f:
                files = {"video": ("edit.mp4", f, "video/mp4")}
                if poster:
                    files["thumbnail"] = ("cover.jpg", open(poster, "rb"), "image/jpeg")
                r = httpx.post(f"{self.base}/sendVideo",
                               data={"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true",
                                     "width": "1080", "height": "1920"},
                               files=files, timeout=180)
            return r.json()
        except Exception as e:
            log.warning("sendVideo failed: %s", e)
            return None
        finally:
            if poster and os.path.exists(poster):
                os.unlink(poster)

    def setup_menu_button(self):
        self.call("setChatMenuButton", menu_button={"type": "web_app", "text": "Эдиты", "web_app": {"url": self.webapp_url}})

    # ---------- приём сообщений ----------
    def start_polling(self):
        threading.Thread(target=self._poll, daemon=True, name="tg-poll").start()

    def _poll(self):
        offset = 0
        while True:
            try:
                r = httpx.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60).json()
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if (msg.get("text") or "").startswith("/start") and self.on_start:
                        self.on_start(msg["from"])
            except Exception as e:
                log.warning("poll error: %s", e)
                time.sleep(5)
