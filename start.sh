#!/usr/bin/env bash

# Устанавливаем браузер для Playwright при каждом запуске контейнера (или проверяем его наличие)
playwright install chromium

# Запускаем чекер ДТЭК в фоновом режиме
python dtek_checker.py &

# Запускаем основной бот в основном потоке
python main.py