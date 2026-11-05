import asyncio
import datetime
import os
import sys
from collections import deque
from zoneinfo import ZoneInfo
from ping3 import ping
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import psycopg2

# --- БУФЕР ЛОГІВ ---
log_buffer = deque(maxlen=2000)

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

last_check_time = "Еще не проводилась"

def get_kyiv_time():
    return datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%Y-%m-%d %H:%M:%S")

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в переменных окружения!")
    return psycopg2.connect(DATABASE_URL)

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📈 График за 7 дней")]
        ],
        resize_keyboard=True
    )

def get_logs_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ 10 минут", callback_data="logs_10"),
                InlineKeyboardButton(text="⏱ 60 минут", callback_data="logs_60"),
                InlineKeyboardButton(text="⏱ 24 часа", callback_data="logs_1440")
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
    print("[БАЗА ДАННЫХ] Инициализация PostgreSQL в Supabase прошла успешно.", flush=True)

def add_user(chat_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[ОШИБКА БД] Не удалось добавить пользователя: {e}", flush=True)

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
        print(f"[ОШИБКА БД] Не удалось получить список пользователей: {e}", flush=True)
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
        print(f"[БАЗА ДАННЫХ] Записано событие -> Статус: {status} | Время: {now}", flush=True)
    except Exception as e:
        print(f"[ОШИБКА БД] Не удалось записать событие: {e}", flush=True)

def log_event_silent(status, custom_time=None):
    now = custom_time if custom_time else datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO events (timestamp, status) VALUES (%s, %s);", (now, status))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[ТИХИЙ БЭКЕНД] Записано в БД -> Статус: {status} | Время: {now}", flush=True)
    except Exception as e:
        print(f"[ОШИБКА БД] Не удалось выполнить тихую запись: {e}", flush=True)

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
        print(f"[ОШИБКА БД] Ошибка get_last_event: {e}", flush=True)
        return None

def format_duration(seconds):
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} ч.")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} мин.")
    parts.append(f"{secs} сек.")
    return " ".join(parts)

async def ping_target(ip):
    global last_check_time
    last_check_time = get_kyiv_time()
    
    try:
        delay = ping(ip, timeout=3, unit='ms')
        is_success = delay is not None and delay is not False
        
        if is_success:
            ms_val = round(delay, 1)
            print(f"[ПИНГ] Проверка {ip} -> Успешно | Отклик: {ms_val} ms | TTL: 64", flush=True)
            return True
        else:
            print(f"[ПИНГ] Проверка {ip} -> Неуспешно (Нет ответа)", flush=True)
            return False
    except Exception as e:
        print(f"[ПИНГ] Ошибка выполнения пинга: {e}", flush=True)
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
            print(f"[ОШИБКА] Не удалось отправить сообщение {uid}: {e}", flush=True)

# --- ГЕНЕРАЦИЯ ГРАФИКА (СУПЕР-ЧИТАЕМЫЙ ВИД: 7 СТРОК, КАЖДАЯ ОТ 00:00 ДО 23:59) ---
def generate_chart():
    print("[ГРАФИК] Генерация чистого таймлайна по дням...", flush=True)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, status FROM events ORDER BY timestamp ASC;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
        # 7 дней: от 6 дней назад до сегодня
        days = [(now_kyiv - datetime.timedelta(days=6 - i)).date() for i in range(7)]
        
        fig, axes = plt.subplots(7, 1, figsize=(12, 10), sharex=True)

        colors = {
            "ONLINE": "#2ecc71",   # Зеленый (Есть свет)
            "OFFLINE": "#e74c3c",  # Красный (Нет света)
            "UNKNOWN": "#2c3e50"   # Серый (Нет данных)
        }

        for idx, day in enumerate(days):
            ax = axes[idx]
            day_start = datetime.datetime.combine(day, datetime.time.min)
            day_end = datetime.datetime.combine(day, datetime.time.max)

            ax.set_xlim(day_start, day_end)
            ax.set_ylim(0, 1)

            day_events = [r for r in rows if r[0].date() == day]

            if not day_events:
                # Если за день вообще нет записей — заливаем весь день серым (Нет данных)
                ax.axvspan(day_start, day_end, color=colors["UNKNOWN"], alpha=0.3)
            else:
                # Определяем статус на начало дня (берем последнее событие до начала этого дня)
                past_events = [r for r in rows if r[0] < day_start]
                last_st = "UNKNOWN"
                if past_events:
                    last_st = past_events[-1][1]

                # Собираем интервалы
                intervals = []
                current_time = day_start
                current_status = last_st

                for ev in day_events:
                    ev_time = ev[0]
                    if ev_time > current_time:
                        intervals.append((current_time, ev_time, current_status))
                    current_time = ev_time
                    current_status = ev[1]

                if current_time < day_end:
                    intervals.append((current_time, day_end, current_status))

                # Рисуем каждый интервал на шкале этого дня
                for start_t, end_t, status in intervals:
                    color = colors.get(status, colors["UNKNOWN"])
                    ax.axvspan(start_t, end_t, color=color, alpha=0.6)

            # Настройка оси Y для каждого дня
            ax.set_yticks([0.5])
            ax.set_yticklabels([day.strftime("%d.%m")], fontsize=10, fontweight="bold")
            ax.grid(True, linestyle="--", alpha=0.4, axis='x')
            ax.tick_params(left=False, labelleft=True)

        # Форматирование общей оси X внизу (часы от 00:00 до 23:59)
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=ZoneInfo("Europe/Kiev")))
        axes[-1].xaxis.set_major_locator(mdates.HourLocator(interval=3))

        plt.suptitle("График наличия света за последние 7 дней", fontsize=14, fontweight="bold", y=0.95)
        plt.tight_layout()
        
        chart_path = "power_chart_7days.png"
        plt.savefig(chart_path, dpi=150)
        plt.close()
        
        print("[ГРАФИК] График успешно сгенерирован в классическом формате.", flush=True)
        return chart_path
    except Exception as e:
        print(f"[ОШИБКА ГРАФИКА] {e}", flush=True)
        return None

# --- ОБРАБОТКА КОМАНД И КНОПОК ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.chat.id)
    await message.answer("⚡ Привет! Бот мониторинга сети успешно подключен.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 Статус")
@dp.message(Command("status"))
async def cmd_status(message: Message):
    add_user(message.chat.id)
    last = get_last_event()
    if not last:
        text = "📊 История пустая, мониторинг только запущен."
    else:
        t, status = last
        st_text = "🟢 Свет есть" if status == "ONLINE" else "🔴 Света нет"
        text = f"Текущий статус: {st_text}\nСвет {'есть' if status == 'ONLINE' else 'отсутствует'} с: {t}\nПоследняя проверка: {last_check_time}"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📈 График за 7 дней")
@dp.message(Command("chart"))
async def cmd_chart(message: Message):
    add_user(message.chat.id)
    chart_file = generate_chart()
    caption = "📈 График наличия света по дням (последние 7 дней):"
    if chart_file and os.path.exists(chart_file):
        photo = BufferedInputFile.from_file(chart_file)
        await message.answer_photo(photo=photo, caption=caption, reply_markup=get_main_keyboard())
    else:
        await message.answer("Недостаточно данных для построения графика.", reply_markup=get_main_keyboard())

@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    add_user(message.chat.id)
    await message.answer("🛠 Выберите период для выгрузки логов:", reply_markup=get_logs_keyboard())

@dp.callback_query(F.data.startswith("logs_"))
async def process_logs_callback(callback: CallbackQuery):
    minutes = int(callback.data.split("_")[1])
    now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
    threshold_time = now_kyiv - datetime.timedelta(minutes=minutes)

    filtered_logs = [item[1] for item in log_buffer if item[0] >= threshold_time]

    if not filtered_logs:
        await callback.message.answer(f"📭 За последние {minutes if minutes < 1440 else '24 часа'} логов нет.")
        await callback.answer()
        return

    logs_text = "\n".join(filtered_logs)
    file_bytes = logs_text.encode("utf-8")
    filename = f"logs_{minutes}min_{now_kyiv.strftime('%Y%m%d_%H%M%S')}.txt"

    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📄 Логи за последние {minutes if minutes < 1440 else '24 часа'}."
    )
    await callback.answer()

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
    print("[МОНИТОРИНГ] Цикл мониторинга запущен.", flush=True)

    while True:
        print(f"\n[МОНИТОРИНГ] Начинаю проверку IP {TARGET_IP}...", flush=True)
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

                text = f"🟢 Свет появился в {recovery_time_str}. Света не было {duration_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())
            else:
                print("[МОНИТОРИНГ] Сеть стабильна, свет есть.", flush=True)
        else:
            fails += 1
            print(f"[МОНИТОРИНГ] Сбой пинга: {fails}/{FAIL_THRESHOLD}", flush=True)
            if is_online and fails >= FAIL_THRESHOLD:
                is_online = False
                outage_start_time = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
                log_event("OFFLINE") 

                current_time_str = outage_start_time.strftime("%H:%M")
                text = f"🔴 Свет пропал в {current_time_str}."
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
    print(f"[WEB] Веб-сервер запущен на порту {port}", flush=True)

async def main():
    print("[СТАРТ] Инициализация...", flush=True)
    init_db()
    await start_web_server()
    asyncio.create_task(monitor_loop())
    print("[СТАРТ] Запуск пуллинга...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())