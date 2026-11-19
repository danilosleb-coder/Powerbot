import asyncio
import datetime
import hashlib
import json
import os
from zoneinfo import ZoneInfo
import aiohttp
import psycopg2
from playwright.async_api import async_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
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


def get_all_users():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Не удалось получить пользователей: {e}", flush=True)
        return []


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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_notifications (
                notif_key VARCHAR(128) PRIMARY KEY,
                sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
            );
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("[ДТЭК БД] Таблицы dtek_schedules и sent_notifications готовы.", flush=True)
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Не удалось инициализировать таблицы: {e}", flush=True)


def claim_notification(notif_key: str) -> bool:
    """
    Атомарно бронирует ключ уведомления в БД.
    Возвращает True, если уведомление ещё не отправлялось, и False — если дубль.
    """
    if not DATABASE_URL:
        return True
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = get_kyiv_time().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO sent_notifications (notif_key, sent_at)
            VALUES (%s, %s)
            ON CONFLICT (notif_key) DO NOTHING
            RETURNING notif_key;
        """, (notif_key, now))
        inserted = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return inserted is not None
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Ошибка claim_notification: {e}", flush=True)
        return True


def get_latest_schedule_for_date(date_str: str):
    if not DATABASE_URL:
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT schedule_json
            FROM dtek_schedules
            WHERE schedule_date = %s
            ORDER BY id DESC
            LIMIT 1;
        """, (date_str,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Ошибка чтения графика: {e}", flush=True)
        return None


def save_schedule_to_db(schedule_date_str, schedule_data, is_tomorrow=False):
    if not DATABASE_URL or not schedule_data:
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
        print(f"[ДТЭК БД] График на {schedule_date_str} (is_tomorrow={is_tomorrow}) сохранён.", flush=True)
    except Exception as e:
        print(f"[ДТЭК ОШИБКА БД] Ошибка сохранения графика: {e}", flush=True)


async def broadcast_telegram_message(text, notif_key: str | None = None):
    if not BOT_TOKEN:
        print("[ОШИБКА ТГ] BOT_TOKEN не задан.", flush=True)
        return
    if notif_key and not claim_notification(notif_key):
        print(f"[ДТЭК] Пропущен дубль уведомления: {notif_key}", flush=True)
        return

    users = get_all_users()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        for uid in users:
            payload = {"chat_id": uid, "text": text, "parse_mode": "Markdown"}
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        print(f"[ОШИБКА ТГ] Не удалось отправить {uid}: {await resp.text()}", flush=True)
            except Exception as e:
                print(f"[ОШИБКА ТГ] Исключение: {e}", flush=True)


async def fetch_dtek_schedule():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
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

            street_input = page.locator("#discon_form #street")
            await street_input.click()
            await street_input.fill("")
            await street_input.type(DTEK_STREET, delay=100)

            dropdown_item = page.locator("#discon_form #streetautocomplete-list div").first
            await dropdown_item.wait_for(state="visible", timeout=8000)
            await dropdown_item.click()
            await asyncio.sleep(0.5)

            house_input = page.locator("#discon_form #house_num")
            await house_input.click()
            await house_input.fill("")
            await house_input.type(DTEK_HOUSE, delay=100)

            house_dropdown = page.locator("#discon_form #house_numautocomplete-list div").first
            await house_dropdown.wait_for(state="visible", timeout=8000)
            await house_dropdown.click()

            schedule_section = page.locator("#discon-fact")
            await schedule_section.wait_for(state="visible", timeout=12000)
            await asyncio.sleep(1.5)

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

            await tabs.nth(0).click()
            await asyncio.sleep(0.5)

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


async def process_fetched_schedule(data, has_tomorrow, notify_on_change=True):
    now_kyiv = get_kyiv_time()
    today_date_str = now_kyiv.strftime("%Y-%m-%d")
    tomorrow_date_str = (now_kyiv + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    keys = list(data.keys())
    today_schedule = data.get(keys[0], []) if len(keys) > 0 else []
    tomorrow_schedule = data.get(keys[1], []) if (has_tomorrow and len(keys) > 1) else []

    if today_schedule:
        prev_today = get_latest_schedule_for_date(today_date_str)
        if prev_today != today_schedule:
            save_schedule_to_db(today_date_str, today_schedule, is_tomorrow=False)
            if notify_on_change and prev_today is not None:
                sched_hash = hashlib.md5(json.dumps(today_schedule, sort_keys=True).encode()).hexdigest()[:12]
                notif_key = f"dtek_today_{today_date_str}_{sched_hash}"
                msg = f"⚡ **Обновился график на сегодня ({keys[0]})!**\nОткройте «📊 График» для просмотра."
                await broadcast_telegram_message(msg, notif_key=notif_key)

    if tomorrow_schedule:
        prev_tomorrow = get_latest_schedule_for_date(tomorrow_date_str)
        if prev_tomorrow != tomorrow_schedule:
            save_schedule_to_db(tomorrow_date_str, tomorrow_schedule, is_tomorrow=True)
            if notify_on_change:
                sched_hash = hashlib.md5(json.dumps(tomorrow_schedule, sort_keys=True).encode()).hexdigest()[:12]
                notif_key = f"dtek_tomorrow_{tomorrow_date_str}_{sched_hash}"
                msg = f"📅 **Появился/обновился график на завтра ({keys[1]})!**\nОткройте «📊 График» для просмотра."
                await broadcast_telegram_message(msg, notif_key=notif_key)


async def run_dtek_check_manual():
    try:
        data, has_tomorrow = await fetch_dtek_schedule()
        if data and len(data) > 0:
            await process_fetched_schedule(data, has_tomorrow, notify_on_change=False)
            return "✅ Проверка графика ДТЭК успешно выполнена!"
        return "⚠️ Не удалось получить данные от ДТЭК."
    except Exception as e:
        return f"❌ Ошибка проверки: {e}"


async def checker_main_loop():
    init_dtek_db()
    print("[ДТЭК ЧЕКЕР] Мониторинг запущен (один экземпляр, с дедупликацией в БД).", flush=True)

    while True:
        try:
            print("\n----------------------------------------", flush=True)
            print("[ДТЭК ЧЕКЕР] Проверка сайта ДТЭК...", flush=True)
            data, has_tomorrow = await fetch_dtek_schedule()
            if data and len(data) > 0:
                await process_fetched_schedule(data, has_tomorrow, notify_on_change=True)
        except Exception as e:
            print(f"[ДТЭК ЧЕКЕР ОШИБКА]: {e}", flush=True)

        await asyncio.sleep(1800)