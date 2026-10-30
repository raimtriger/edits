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

## Футажи

Видео кладутся в `/opt/edits/footage/<категория>/`: `night`, `love`, `nature`, `bokeh`, `cinema`, `stock`.
