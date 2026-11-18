import asyncio
import os
import json
import datetime
from zoneinfo import ZoneInfo
import aiohttp
import psycopg2
from playwright.async_api import async_playwright

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9285635116:AAH9tk30qN2xAcdM-ChCtr0kSWKT5XEMPik")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "") # Твой chat_id для алертов
DATABASE_URL = os.getenv("DATABASE_URL")

DTEK_STREET = "Ніжинська"
DTEK_HOUSE = "7"
URL = "https://www.dtek-kem.com.ua/ua/shutdowns"

def get_kyiv_time():
    return datetime.datetime.now(ZoneInfo("Europe/Kiev"))

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в переменных окружения!")
    return psycopg2.connect(DATABASE_URL)

def init_dtek_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dtek_schedules (
                id SERIAL PRIMARY KEY,
                schedule_date DATE NOT NULL,
                fetched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                schedule_json JSONB NOT NULL,
                is_tomorrow BOOLEAN DEFAULT FALSE
            );
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("[ДТЭК БД] Таблица dtek_schedules успешно инициализирована.", flush=True)
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Не удалось создать таблицу: {e}", flush=True)

def save_schedule_to_db(schedule_date_str, schedule_data, is_tomorrow=False):
    """Сохраняет в БД, а если базы нет — красиво выводит график в терминал"""
    
    # 1. Всегда выводим график в терминал для наглядности (чтобы ты видел его глазами)
    day_type = "ЗАВТРА" if is_tomorrow else "СЕГОДНЯ"
    print(f"\n================ ДEТАЛИ ГРАФИКА ({day_type} - {schedule_date_str}) ================", flush=True)
    for item in schedule_data:
        print(f"   {item['hour']}  |  {item['desc']}", flush=True)
    print("========================================================\n", flush=True)

    # 2. Пытаемся записать в БД, если она настроена
    if not DATABASE_URL:
        print(f"[ДТЭК ЛОКАЛ] База данных не подключена (нет DATABASE_URL). Пропускаем запись в БД.", flush=True)
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = get_kyiv_time().strftime("%Y-%m-%d %H:%M:%S")
        json_data = json.dumps(schedule_data, ensure_ascii=False)

        cursor.execute("""
            INSERT INTO dtek_schedules (schedule_date, fetched_at, schedule_json, is_tomorrow)
            VALUES (%s, %s, %s, %s);
        """, (schedule_date_str, now, json_data, is_tomorrow))
        
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[ДТЭК БД] График на {schedule_date_str} успешно сохранен в базу.", flush=True)
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Ошибка сохранения графика: {e}", flush=True)

async def send_telegram_message(text):
    if not TARGET_CHAT_ID:
        print(f"[ТЕЛЕГРАМ (Лог)] {text}", flush=True)
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TARGET_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    print(f"[ОШИБКА ТГ] {await resp.text()}", flush=True)
        except Exception as e:
            print(f"[ОШИБКА ТГ] {e}", flush=True)

async def fetch_dtek_schedule():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()
        try:
            print("[ДТЭК ПАРСЕР] Открываем сайт ДТЭК...", flush=True)
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            
            try:
                close_btn = page.locator("button.modal__close, .btn-close").first
                if await close_btn.is_visible(timeout=2000):
                    await close_btn.click()
            except Exception:
                pass

            # Ввод улицы
            street_input = page.locator("#discon_form #street")
            await street_input.click()
            await street_input.fill("")
            await street_input.type(DTEK_STREET, delay=100)
            
            dropdown_item = page.locator("#discon_form #streetautocomplete-list div").first
            await dropdown_item.wait_for(state="visible", timeout=8000)
            await dropdown_item.click()
            await asyncio.sleep(0.5)

            # Ввод дома
            house_input = page.locator("#discon_form #house_num")
            await house_input.click()
            await house_input.fill("")
            await house_input.type(DTEK_HOUSE, delay=100)
            
            house_dropdown = page.locator("#discon_form #house_numautocomplete-list div").first
            await house_dropdown.wait_for(state="visible", timeout=8000)
            await house_dropdown.click()

            # Ожидание таблицы
            schedule_section = page.locator("#discon-fact")
            await schedule_section.wait_for(state="visible", timeout=12000)
            await asyncio.sleep(1.5)

            # Проверяем вкладку "на завтра"
            tabs = page.locator(".dates .date")
            count = await tabs.count()
            has_tomorrow = False
            
            if count > 1:
                tomorrow_tab = tabs.nth(1)
                await tomorrow_tab.click()
                await asyncio.sleep(1)
                page_text = await page.inner_text("body")
                if "ще не надходила" not in page_text:
                    has_tomorrow = True

            await tabs.nth(0).click() # Возврат на сегодня
            await asyncio.sleep(0.5)

            # Парсим графики (и сегодня, и завтра, если оно есть) через JS
            raw_data = await page.evaluate("""() => {
                const result = {};
                const tabs = document.querySelectorAll('.dates .date');
                const tables = document.querySelectorAll('#discon-fact .discon-fact-table');

                tables.forEach((table, idx) => {
                    const tabText = tabs[idx] ? tabs[idx].innerText.trim().replace(/\\s+/g, ' ') : `День ${idx + 1}`;
                    const hours = [];
                    const ths = table.querySelectorAll('thead th');
                    const tds = table.querySelectorAll('tbody tr td');

                    let hourLabels = [];
                    ths.forEach(th => {
                        const txt = th.innerText.trim();
                        if (txt.includes('-')) hourLabels.push(txt);
                    });

                    const statusCells = Array.from(tds).slice(-24);

                    hourLabels.forEach((label, i) => {
                        const cell = statusCells[i];
                        if (!cell) return;
                        const cls = cell.className;

                        let status = 'ON';
                        let desc = '🟢 Свет есть';
                        if (cls.includes('cell-first-half')) { status = 'OFF_FIRST_30'; desc = '🌗 Нет первые 30 мин'; }
                        else if (cls.includes('cell-second-half')) { status = 'OFF_SECOND_30'; desc = '🌗 Нет вторые 30 мин'; }
                        else if (cls.includes('cell-scheduled') || (!cls.includes('cell-non-scheduled') && cls.trim() !== '')) { status = 'OFF'; desc = '🔴 Света нет'; }

                        hours.push({ hour: label, status: status, desc: desc });
                    });
                    result[tabText] = hours;
                });
                return result;
            }""")

            await browser.close()
            return raw_data, has_tomorrow

        except Exception as e:
            print(f"[ДТЭК ОШИБКА ПАРСИНГА]: {e}", flush=True)
            await browser.close()
            return None, False

async def checker_main_loop():
    init_dtek_db()
    print("[ДТЭК ЧЕКЕР] Мониторинг запущен с записью в PostgreSQL.", flush=True)
    
    last_today_schedule = None
    last_tomorrow_published = False

    while True:
        try:
            print("\n----------------------------------------", flush=True)
            print("[ДТЭК ЧЕКЕР] Проверка сайта ДТЭК...", flush=True)
            data, has_tomorrow = await fetch_dtek_schedule()

            if data and len(data) > 0:
                keys = list(data.keys())
                
                # 1. Сегодня
                today_key = keys[0]
                today_schedule = data.get(today_key, [])
                
                # Вытаскиваем дату из названия вкладки (например, "на сьогодні 18.11.26" -> превратим в дату или сохраним ключ)
                today_date_str = get_kyiv_time().strftime("%Y-%m-%d")

                if today_schedule:
                    if last_today_schedule is None:
                        last_today_schedule = today_schedule
                        save_schedule_to_db(today_date_str, today_schedule, is_tomorrow=False)
                        print(f"[ДТЭК] Записан базовый график на сегодня ({today_key}).", flush=True)
                    elif last_today_schedule != today_schedule:
                        last_today_schedule = today_schedule
                        save_schedule_to_db(today_date_str, today_schedule, is_tomorrow=False)
                        print(f"[ДТЭК] Обнаружен новый график на сегодня! Сохранено в БД.", flush=True)
                        
                        msg = f"⚡ **Внимание! Обновился график на сегодня ({today_key})!**\n\n"
                        for item in today_schedule:
                            msg += f"`{item['hour']}` | {item['desc']}\n"
                        await send_telegram_message(msg)
                    else:
                        print(f"[ДТЭК] Изменений на сегодня нет.", flush=True)

                # 2. Завтра
                if has_tomorrow and len(keys) > 1:
                    tomorrow_key = keys[1]
                    tomorrow_schedule = data.get(tomorrow_key, [])
                    tomorrow_date_str = (get_kyiv_time() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

                    if not last_tomorrow_published:
                        last_tomorrow_published = True
                        save_schedule_to_db(tomorrow_date_str, tomorrow_schedule, is_tomorrow=True)
                        print(f"[ДТЭК] Появился график на завтра ({tomorrow_key})! Сохранено в БД.", flush=True)
                        
                        msg = f"📅 **График на завтра ({tomorrow_key}):**\n\n"
                        for item in tomorrow_schedule:
                            msg += f"`{item['hour']}` | {item['desc']}\n"
                        await send_telegram_message(msg)
                    else:
                        print(f"[ДТЭК] График на завтра уже зафиксирован ранее.", flush=True)
                else:
                    last_tomorrow_published = False
                    print(f"[ДТЭК] Информация на завтра еще не опубликована.", flush=True)

            else:
                print("[ДТЭК ЧЕКЕР] Не удалось получить данные.", flush=True)

        except Exception as e:
            print(f"[ДТЭК ЧЕКЕР ОШИБКА]: {e}", flush=True)

        print("[ДТЭК ЧЕКЕР] Ожидание следующей проверки (30 минут)...", flush=True)
        await asyncio.sleep(1800)

if __name__ == "__main__":
    asyncio.run(checker_main_loop())