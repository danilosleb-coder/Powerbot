#!/usr/bin/env bash

# 1. Устанавливаем браузер для Playwright
playwright install chromium

# 2. Запускаем чекер ДТЭК в фоне, но с небольшой задержкой (пусть сначала поднимется бот)
python dtek_checker.py &

# 3. Запускаем основной бот и веб-сервер в главном потоке (Render следит именно за ним)
python main.py