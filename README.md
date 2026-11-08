# LUDUS EDITS

Telegram мини-аппка: артист грузит трек — сервер распознаёт текст, находит припев и монтирует эдиты 1080×1920.

- `index.html` — интерфейс. С GitHub Pages работает как демо (плюс монтаж в браузере), с сервера — по-настоящему.
- `server/` — FastAPI + очередь монтажа + бот (`main.py`, `tg.py`, `engine/render.py`, `engine/align.py`).
- `deploy/` — установка и откат.

## Установка на сервер

```
git clone https://github.com/raimtriger/edits /opt/edits && bash /opt/edits/deploy/install.sh <BOT_TOKEN>
```

## Обновление

```
cd /opt/edits && git pull && venv/bin/pip install -q -r server/requirements.txt && systemctl restart edits
```

## Откат

```
bash /opt/edits/deploy/uninstall.sh
```

## Открытие из России: фронт на GitHub Pages

Из РФ большие ответы через Cloudflare/Амстердам обрываются (~20 КБ), и аппка в Telegram висит на загрузочном экране.
Поэтому страница отдаётся с GitHub Pages, а API остаётся на сервере (`https://app.ludusedits.ru/`).

- Pages: Settings → Pages → Deploy from a branch → `main` / `/ (root)`. Адрес: `https://raimtriger.github.io/edits/`
- `tg.js` лежит в репо (скрипт Telegram, telegram.org из РФ часто недоступен). Обновить: скопировать `/opt/edits/data/telegram-web-app.js`.
- В `.env` на сервере: `WEBAPP_URL=https://raimtriger.github.io/edits/`; адрес страницы для CORS — `CORS_ORIGINS` (по умолчанию `https://raimtriger.github.io`).
- Страница сама понимает, где открыта: на `app.ludusedits.ru` API на том же хосте, на любом другом — идёт на `https://app.ludusedits.ru/`.
- Откат на старую схему: в `.env` вернуть `WEBAPP_URL=https://app.ludusedits.ru/` и `systemctl restart edits`.

## Футажи

Видео кладутся в `/opt/edits/footage/<категория>/`: `night`, `love`, `nature`, `bokeh`, `cinema`, `stock`.
