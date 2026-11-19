#!/usr/bin/env bash
set -e

# 1. Устанавливаем браузер для Playwright (если ещё не установлен)
playwright install chromium --with-deps || playwright install chromium

# 2. Запускаем только главный процесс бота + веб-сервера (чекер ДТЭК запускается внутри main.py без дубля)
exec python main.py