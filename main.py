import asyncio
import datetime
import os
import sys
from collections import deque
from zoneinfo import ZoneInfo
from ping3 import ping
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import psycopg2

# --- БУФЕР ЛОГІВ (зберігаємо з мітками часу) ---
log_buffer = deque(maxlen=2000)  # Збільшили буфер, щоб вистачало на 24 години

class LogInterceptor:
    def __init__(self):
        self.terminal = sys.stdout
    def write(self, message):
        self.terminal.write(message)
        self.terminal.flush()
        
        text = message.strip()
        if text:
            now_dt = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
            time_prefix = now_dt.strftime("[%H:%M:%S]")
            log_buffer.append((now_dt, f"{time_prefix} {text}"))
            
    def flush(self):
        self.terminal.flush()

sys.stdout = LogInterceptor()

# --- НАЛАШТУВАННЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9285635116:AAH9tk30qN2xAcdM-ChCtr0kSWKT5XEMPik")
TARGET_IP = "176.36.251.189"
DATABASE_URL = os.getenv("DATABASE_URL")

CHECK_INTERVAL = 30
FAIL_THRESHOLD = 2

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

last_check_time = "Ще не проводилась"

def get_kyiv_time():
    return datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%Y-%m-%d %H:%M:%S")

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не знайдено в змінних оточення!")
    return psycopg2.connect(DATABASE_URL)

# --- ПОСТІЙНА КЛАВІАТУРА ЗНИЗУ (як на скріншоті) ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📈 Графік за 7 днів")],
            [KeyboardButton(text="📄 Отримати логи")]
        ],
        resize_keyboard=True
    )

# --- КЛАВІАТУРА ВИБОРУ ЧАСУ ЛОГІВ ---
def get_logs_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ 10 хвилин", callback_data="logs_10"),
                InlineKeyboardButton(text="⏱ 60 хвилин", callback_data="logs_60"),
                InlineKeyboardButton(text="⏱ 24 години", callback_data="logs_1440")
            ]
        ]
    )

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            status VARCHAR(10) NOT NULL
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY
        );
    """)
    conn.commit()
    cursor.close()
    conn.close()
    print("[БАЗА ДАНИХ] Ініціалізація PostgreSQL у Supabase пройшла успішно.", flush=True)

def add_user(chat_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[ОШИБКА БД] Не вдалося додати користувача: {e}", flush=True)

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
        print(f"[ОШИБКА БД] Не вдалося отримати список користувачів: {e}", flush=True)
        return []

def log_event(status):
    now = datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO events (timestamp, status) VALUES (%s, %s);", (now, status))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[БАЗА ДАНИХ] Записано подію -> Статус: {status} | Час: {now}", flush=True)
    except Exception as e:
        print(f"[ОШИБКА БД] Не вдалося записати подію: {e}", flush=True)

def get_last_event():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, status FROM events ORDER BY id DESC LIMIT 1;")
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            t_str = row[0].strftime("%Y-%m-%d %H:%M:%S") if isinstance(row[0], datetime.datetime) else str(row[0])
            return t_str, row[1]
        return None
    except Exception as e:
        print(f"[ОШИБКА БД] Помилка get_last_event: {e}", flush=True)
        return None

def format_duration(seconds):
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} год.")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} хв.")
    parts.append(f"{secs} сек.")
    return " ".join(parts)

async def ping_target(ip):
    global last_check_time
    last_check_time = get_kyiv_time()
    
    try:
        response = ping(ip, timeout=3)
        is_success = response is not None and response is not False
        print(f"[ПІНГ] Перевірка {ip} -> {'Успішно (Світло є)' if is_success else 'Неуспішно (Немає відповіді)'}", flush=True)
        return is_success
    except Exception as e:
        print(f"[ПІНГ] Помилка виконання пінга: {e}", flush=True)
        return False

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
            print(f"[ОШИБКА] Не вдалося надіслати повідомлення {uid}: {e}", flush=True)

# --- ГЕНЕРАЦІЯ ГРАФІКА ЗА 7 ДНІВ ---
def generate_chart():
    print("[ГРАФІК] Генерація графіка за останні 7 днів...", flush=True)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
        start_date = (now_kyiv - datetime.timedelta(days=6)).date()
        start_dt = datetime.datetime.combine(start_date, datetime.time.min)
        
        cursor.execute("SELECT timestamp, status FROM events WHERE timestamp >= %s ORDER BY timestamp ASC;", (start_dt,))
        rows = cursor.fetchall()

        days = [(now_kyiv - datetime.timedelta(days=6 - i)).date() for i in range(7)]
        fig, axes = plt.subplots(7, 1, figsize=(10, 10), sharex=True)

        for idx, day in enumerate(days):
            ax = axes[idx]
            day_start = datetime.datetime.combine(day, datetime.time.min)
            day_end = datetime.datetime.combine(day, datetime.time.max)

            cursor.execute("SELECT status FROM events WHERE timestamp < %s ORDER BY timestamp DESC LIMIT 1;", (day_start,))
            initial_row = cursor.fetchone()
            initial_status = 1 if (initial_row and initial_row[0] == "ONLINE") else 0

            day_events = [r for r in rows if r[0].date() == day]

            plot_times = [day_start]
            plot_statuses = [initial_status]

            for ev in day_events:
                plot_times.append(ev[0])
                plot_statuses.append(1 if ev[1] == "ONLINE" else 0)

            plot_times.append(day_end)
            plot_statuses.append(plot_statuses[-1])

            ax.step(plot_times, plot_statuses, where='post', color='#2ecc71', linewidth=2)
            ax.fill_between(plot_times, plot_statuses, step='post', color='#2ecc71', alpha=0.2)

            ax.set_yticks([0, 1])
            ax.set_yticklabels(["Ні", "Є"], fontsize=8)
            ax.set_ylim(-0.1, 1.1)
            ax.set_ylabel(day.strftime("%d.%m"), fontsize=9, rotation=0, labelpad=25, va='center')
            ax.grid(True, linestyle="--", alpha=0.5)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.set_xlim(day_start, day_end)

        cursor.close()
        conn.close()

        plt.suptitle("Графік наявності світла за останні 7 днів", fontsize=14, fontweight="bold", y=0.98)
        plt.tight_layout()

        chart_path = "power_chart_7days.png"
        plt.savefig(chart_path, dpi=150)
        plt.close()
        print("[ГРАФІК] Графік успішно збережено.", flush=True)
        return chart_path
    except Exception as e:
        print(f"[ОШИБКА ГРАФІКА] Не вдалося згенерувати графік: {e}", flush=True)
        return None

# --- ОБРОБКА КОМАНД ТА КНОПОК ЗНИЗУ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.chat.id)
    await message.answer("⚡ Привіт! Бот моніторингу мережі успішно підключений.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 Статус")
@dp.message(Command("status"))
async def cmd_status(message: Message):
    add_user(message.chat.id)
    last = get_last_event()
    if not last:
        text = "📊 Історія порожня, моніторинг тільки запущено."
    else:
        t, status = last
        st_text = "🟢 Світло є" if status == "ONLINE" else "🔴 Світла немає"
        text = f"Поточний статус: {st_text}\nСвітло {'є' if status == 'ONLINE' else 'відсутнє'} з: {t}\nОстання перевірка: {last_check_time}"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📈 Графік за 7 днів")
@dp.message(Command("chart"))
async def cmd_chart(message: Message):
    add_user(message.chat.id)
    chart_file = generate_chart()
    caption = "📈 Графік наявності світла по днях (останні 7 днів):"
    if chart_file and os.path.exists(chart_file):
        photo = BufferedInputFile.from_file(chart_file)
        await message.answer_photo(photo=photo, caption=caption, reply_markup=get_main_keyboard())
    else:
        await message.answer("Недостатньо даних для побудови графіка.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📄 Отримати логи")
@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    add_user(message.chat.id)
    await message.answer("🛠 Оберіть період для вивантаження логів:", reply_markup=get_logs_keyboard())

# --- ОБРОБКА КНОПОК ВИБОРУ ЛОГІВ ---
@dp.callback_query(F.data.startswith("logs_"))
async def process_logs_callback(callback: CallbackQuery):
    minutes = int(callback.data.split("_")[1])
    now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
    threshold_time = now_kyiv - datetime.timedelta(minutes=minutes)

    filtered_logs = [item[1] for item in log_buffer if item[0] >= threshold_time]

    if not filtered_logs:
        await callback.message.answer(f"📭 За останні {minutes if minutes < 1440 else '24 години'} логів немає.")
        await callback.answer()
        return

    logs_text = "\n".join(filtered_logs)
    file_bytes = logs_text.encode("utf-8")
    filename = f"logs_{minutes}min_{now_kyiv.strftime('%Y%m%d_%H%M%S')}.txt"

    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📄 Логи за останні {minutes if minutes < 1440 else '24 години'}."
    )
    await callback.answer()

# --- ОСНОВНИЙ ЦИКЛ МОНІТОРИНГУ ---
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
    print("[МОНІТОРИНГ] Цикл моніторингу запущено.", flush=True)

    while True:
        print(f"\n[МОНІТОРИНГ] Починаю перевірку IP {TARGET_IP}...", flush=True)
        status = await ping_target(TARGET_IP)

        if status:
            fails = 0
            if not is_online:
                recovery_time_str = datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%H:%M")
                if outage_start_time:
                    down_duration = (datetime.datetime.now(ZoneInfo("Europe/Kiev")) - outage_start_time.replace(tzinfo=ZoneInfo("Europe/Kiev"))).total_seconds()
                else:
                    down_duration = 60 

                duration_str = format_duration(down_duration)
                is_online = True
                log_event("ONLINE")
                outage_start_time = None

                text = f"🟢 Світло з'явилося о {recovery_time_str}. Світла не було {duration_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())
            else:
                print("[МОНІТОРИНГ] Мережа стабільна, світло є.", flush=True)
        else:
            fails += 1
            print(f"[МОНІТОРИНГ] Збій пінга: {fails}/{FAIL_THRESHOLD}", flush=True)
            if is_online and fails >= FAIL_THRESHOLD:
                is_online = False
                outage_start_time = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
                log_event("OFFLINE")

                current_time_str = outage_start_time.strftime("%H:%M")
                text = f"🔴 Світло зникло о {current_time_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())

        await asyncio.sleep(CHECK_INTERVAL)

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
    print(f"[WEB] Веб-сервер запущено на порту {port}", flush=True)

async def main():
    print("[СТАРТ] Ініціалізація...", flush=True)
    init_db()
    await start_web_server()
    asyncio.create_task(monitor_loop())
    print("[СТАРТ] Запуск пулінгу...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())