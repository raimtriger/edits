"""Telegram: проверка initData мини-аппки, отправка видео, приём /start."""
from __future__ import annotations

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

    def send_video(self, chat_id: int, path: str, caption: str = ""):
        try:
            with open(path, "rb") as f:
                r = httpx.post(f"{self.base}/sendVideo",
                               data={"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true",
                                     "width": "1080", "height": "1920"},
                               files={"video": ("edit.mp4", f, "video/mp4")}, timeout=180)
            return r.json()
        except Exception as e:
            log.warning("sendVideo failed: %s", e)
            return None

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
