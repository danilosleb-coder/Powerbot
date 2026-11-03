import asyncio
import datetime
import os
import sqlite3
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9285635116:AAH9tk30qN2xAcdM-ChCtr0kSWKT5XEMPik")
TARGET_IP = "176.36.251.189"

CHECK_INTERVAL = 30
FAIL_THRESHOLD = 2

DB_FILE = "power_log.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальная переменная для хранения времени последней проверки
last_check_time = "Ещё не проводилась"

def get_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="btn_status"),
                InlineKeyboardButton(text="📈 График за 7 дней", callback_data="btn_chart")
            ]
        ]
    )

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()
    print("[БАЗА ДАННЫХ] Инициализация БД прошла успешно.")

def add_user(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def log_event(status):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO events (timestamp, status) VALUES (?, ?)", (now, status))
    conn.commit()
    conn.close()
    print(f"[БАЗА ДАННЫХ] Записано событие -> Статус: {status} | Время: {now}")

def get_last_event():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, status FROM events ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row

def format_duration(seconds):
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} час{'ов' if hours > 4 else ('а' if hours > 1 else '')}")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} мин.")
    parts.append(f"{secs} сек.")
    return " ".join(parts)

async def ping_target(ip):
    global last_check_time
    last_check_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    param = "-n" if os.name == "nt" else "-c"
    command = ["ping", param, "1", "-w", "3000" if os.name == "nt" else "3", ip]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, 
            stdout=asyncio.subprocess.DEVNULL, 
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
        is_success = (process.returncode == 0)
        print(f"[ПИНГ] Проверка {ip} -> {'Успешно (Свет есть)' if is_success else 'Неуспешно (Нет ответа)'}")
        return is_success
    except Exception as e:
        print(f"[ПИНГ] Ошибка выполнения пинга: {e}")
        return False

# --- РАССЫЛКА СООБЩЕНИЙ ВСЕМ ПОЛЬЗОВАТЕЛЯМ ---
async def broadcast_message(text, reply_markup=None, photo_path=None):
    users = get_all_users()
    for uid in users:
        try:
            if photo_path and os.path.exists(photo_path):
                photo = BufferedInputFile.from_file(photo_path)
                await bot.send_photo(chat_id=uid, photo=photo, caption=text, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=uid, text=text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[ОШИБКА] Не удалось отправить сообщение пользователю {uid}: {e}")

# --- ГЕНЕРАЦИЯ ГРАФИКА ЗА 7 ДНЕЙ ---
def generate_chart():
    print("[ГРАФИК] Генерация графика за последние 7 дней...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    seven_days_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT timestamp, status FROM events WHERE timestamp >= ? ORDER BY id ASC", (seven_days_ago,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("[ГРАФИК] Недостаточно данных для генерации.")
        return None

    days_data = {}
    for i in range(7):
        day = (datetime.datetime.now() - datetime.timedelta(days=6 - i)).date()
        days_data[day] = []

    for r in rows:
        dt = datetime.datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
        day = dt.date()
        if day in days_data:
            days_data[day].append((dt, 1 if r[1] == "ONLINE" else 0))

    fig, axes = plt.subplots(7, 1, figsize=(10, 10), sharex=True)
    day_keys = sorted(days_data.keys())
    
    for idx, day in enumerate(day_keys):
        ax = axes[idx]
        events = days_data[day]
        
        if events:
            times = [e[0] for e in events]
            statuses = [e[1] for e in events]
            
            day_start = datetime.datetime.combine(day, datetime.time.min)
            day_end = datetime.datetime.combine(day, datetime.time.max)
            
            plot_times = [day_start] + times + [day_end]
            plot_statuses = [statuses[0]] + statuses + [statuses[-1]]
            
            ax.step(plot_times, plot_statuses, where='post', color='#2ecc71', linewidth=2)
            ax.fill_between(plot_times, plot_statuses, step='post', color='#2ecc71', alpha=0.2)

        ax.set_yticks([0, 1])
        ax.set_yticklabels(["Нет", "Есть"], fontsize=8)
        ax.set_ylim(-0.1, 1.1)
        ax.set_ylabel(day.strftime("%d.%m"), fontsize=9, rotation=0, labelpad=25, va='center')
        ax.grid(True, linestyle="--", alpha=0.5)
        
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.set_xlim(datetime.datetime.combine(day, datetime.time.min), datetime.datetime.combine(day, datetime.time.max))

    plt.suptitle("График наличия света за последние 7 дней", fontsize=14, fontweight="bold", y=0.98)
    plt.tight_layout()

    chart_path = "power_chart_7days.png"
    plt.savefig(chart_path, dpi=150)
    plt.close()
    print("[ГРАФИК] График успешно сохранен.")
    return chart_path

# --- ОБРАБОТКА КОМАНД И КНОПОК ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.chat.id)
    print(f"[ТЕЛЕГРАМ] /start от пользователя: {message.chat.id}")
    await message.answer("⚡ Привет! Бот мониторинга сети успешно подключен.", reply_markup=get_main_keyboard())

@dp.message(Command("status"))
async def cmd_status(message: Message):
    add_user(message.chat.id)
    await send_status_info(message)

async def send_status_info(event_source):
    last = get_last_event()
    if not last:
        text = "📊 История пуста, мониторинг только запущен."
    else:
        t, status = last
        st_text = "🟢 Свет есть" if status == "ONLINE" else "🔴 Света нет"
        
        if status == "ONLINE":
            text = f"Текущий статус: {st_text}\nСвет есть с: {t}\nПоследняя проверка: {last_check_time}"
        else:
            text = f"Текущий статус: {st_text}\nСвета нет с: {t}\nПоследняя проверка: {last_check_time}"
    
    if isinstance(event_source, Message):
        await event_source.answer(text, reply_markup=get_main_keyboard())
    else:
        await event_source.message.answer(text, reply_markup=get_main_keyboard())
        await event_source.answer()

@dp.message(Command("chart"))
async def cmd_chart(message: Message):
    add_user(message.chat.id)
    await send_chart_info(message)

async def send_chart_info(event_source):
    chart_file = generate_chart()
    caption = "📈 График наличия света по дням (последние 7 дней):"
    if chart_file and os.path.exists(chart_file):
        if isinstance(event_source, Message):
            photo = BufferedInputFile.from_file(chart_file)
            await event_source.answer_photo(photo=photo, caption=caption, reply_markup=get_main_keyboard())
        else:
            photo = BufferedInputFile.from_file(chart_file)
            await event_source.message.answer_photo(photo=photo, caption=caption, reply_markup=get_main_keyboard())
            await event_source.answer()
    else:
        text = "Недостаточно данных для построения графика за 7 дней."
        if isinstance(event_source, Message):
            await event_source.answer(text, reply_markup=get_main_keyboard())
        else:
            await event_source.message.answer(text, reply_markup=get_main_keyboard())
            await event_source.answer()

@dp.callback_query(F.data.startswith("btn_"))
async def process_callbacks(callback):
    add_user(callback.message.chat.id)
    if callback.data == "btn_status":
        await send_status_info(callback)
    elif callback.data == "btn_chart":
        await send_chart_info(callback)

# --- ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА ---
async def monitor_loop():
    await asyncio.sleep(5)
    last_event = get_last_event()
    is_online = last_event[1] == "ONLINE" if last_event else True
    outage_start_time = None

    if last_event and last_event[1] == "OFFLINE":
        outage_start_time = datetime.datetime.strptime(last_event[0], "%Y-%m-%d %H:%M:%S")

    if not last_event:
        init_db()
        log_event("ONLINE")

    fails = 0
    print("[МОНИТОРИНГ] Цикл мониторинга запущен.")
    add_user(982012863)
    await broadcast_message("⚡ Бот мониторинга сети успешно запущен.", reply_markup=get_main_keyboard())

    while True:
        print(f"\n[МОНИТОРИНГ] Начинаю проверку IP {TARGET_IP}...")
        status = await ping_target(TARGET_IP)

        if status:
            fails = 0
            if not is_online:
                recovery_time_str = datetime.datetime.now().strftime("%H:%M")
                
                if outage_start_time:
                    down_duration = (datetime.datetime.now() - outage_start_time).total_seconds()
                else:
                    down_duration = 60 

                duration_str = format_duration(down_duration)

                is_online = True
                log_event("ONLINE")
                outage_start_time = None

                text = f"🟢 Свет появился в {recovery_time_str}. Света не было {duration_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())
            else:
                print("[МОНИТОРИНГ] Сеть стабильна, свет есть.")
        else:
            fails += 1
            print(f"[МОНИТОРИНГ] Пинг не прошёл! Сбой подряд: {fails}/{FAIL_THRESHOLD}")
            if is_online and fails >= FAIL_THRESHOLD:
                is_online = False
                outage_start_time = datetime.datetime.now()
                log_event("OFFLINE")

                current_time_str = outage_start_time.strftime("%H:%M")
                text = f"🔴 Свет пропал в {current_time_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())

        print(f"[МОНИТОРИНГ] Ожидание следующей проверки ({CHECK_INTERVAL} сек.)...")
        await asyncio.sleep(CHECK_INTERVAL)

# --- ВЕБ-СЕРВЕР ДЛЯ РАБОТЫ НА RENDER ---
async def handle_web(request):
    return web.Response(text="Power Bot Web Service is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[WEB] Веб-сервер успешно запущен на порту {port}")

async def main():
    print("[СТАРТ] Инициализация приложения...")
    init_db()
    
    # Запускаем веб-сервер для Render и фоновый мониторинг
    await start_web_server()
    asyncio.create_task(monitor_loop())
    
    print("[СТАРТ] Запуск пуллинга Telegram-бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())