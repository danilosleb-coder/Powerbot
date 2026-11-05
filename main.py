import asyncio
import datetime
import json
import os
import sys
from collections import deque
from zoneinfo import ZoneInfo
from ping3 import ping
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo
)
from aiohttp import web
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
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://powerbot-q183.onrender.com/dashboard")

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
            [
                KeyboardButton(text="📊 Статус"),
                KeyboardButton(text="📈 Веб-Дашборд 🌐", web_app=WebAppInfo(url=WEB_APP_URL))
            ]
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

async def broadcast_message(text, reply_markup=None):
    users = get_all_users()
    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[ОШИБКА] Не удалось отправить сообщение {uid}: {e}", flush=True)

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
            fails += 1
            if is_online and fails >= FAIL_THRESHOLD:
                is_online = False
                outage_start_time = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
                log_event("OFFLINE") 

                current_time_str = outage_start_time.strftime("%H:%M")
                text = f"🔴 Свет пропал в {current_time_str}."
                await broadcast_message(text, reply_markup=get_main_keyboard())

        await asyncio.sleep(CHECK_INTERVAL)

# --- ВЕБ-СЕРВЕР И ДАШБОРД С РУССКИМИ ДНЯМИ НЕДЕЛИ И СТАТУСАМИ ---
async def handle_root(request):
    return web.Response(text="Power Bot Web Service is running!")

async def handle_dashboard(request):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, status FROM events ORDER BY timestamp ASC;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[WEB DASHBOARD ERROR] {e}", flush=True)
        rows = []

    now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
    days = [(now_kyiv - datetime.timedelta(days=6 - i)) for i in range(7)]
    
    ru_weekdays = {
        0: 'Понедельник',
        1: 'Вторник',
        2: 'Среда',
        3: 'Четверг',
        4: 'Пятница',
        5: 'Суббота',
        6: 'Воскресенье'
    }

    days_data = []
    for day in days:
        day_date = day.date()
        day_start = datetime.datetime.combine(day_date, datetime.time.min, tzinfo=ZoneInfo("Europe/Kiev"))
        
        # Проверяем, были ли вообще события до этого дня
        past_events = [r for r in rows if (r[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if r[0].tzinfo is None else r[0]) < day_start]
        
        if not past_events and not rows:
            last_st = -1  # Нет инфы вообще (черный)
        elif not past_events:
            last_st = -1  # До первого события в БД нет инфы
        else:
            last_st = 1 if past_events[-1][1] == "ONLINE" else 0
        
        # События текущего дня
        day_events = [r for r in rows if (r[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if r[0].tzinfo is None else r[0]).date() == day_date]
        
        points = [{"time": "00:00", "val": last_st}]
        current_st = last_st
        
        for ev in day_events:
            ev_time = ev[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if ev[0].tzinfo is None else ev[0]
            t_str = ev_time.strftime("%H:%M")
            current_st = 1 if ev[1] == "ONLINE" else 0
            points.append({"time": t_str, "val": current_st})
            
        points.append({"time": "23:59", "val": current_st})
        
        weekday_name = ru_weekdays[day_date.weekday()]
        date_str_formatted = f"{day_date.strftime('%d.%m')} ({weekday_name})"

        days_data.append({
            "date_str": date_str_formatted,
            "points": points
        })

    days_json = json.dumps(days_data)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Powerbot Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
    </head>
    <body class="bg-slate-50 text-slate-800 font-sans antialiased p-3 min-h-screen">
        <div class="max-w-xl mx-auto space-y-3" id="charts-container">
            <!-- Рендеринг графиков -->
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();

            const daysData = {days_json};
            const container = document.getElementById('charts-container');

            daysData.forEach((day, index) => {{
                const card = document.createElement('div');
                card.className = 'bg-white border border-slate-200/80 rounded-xl p-2.5 shadow-sm';
                card.innerHTML = `
                    <div class="flex justify-between items-center mb-1">
                        <span class="text-xs font-bold text-slate-700">${{day.date_str}}</span>
                    </div>
                    <div class="relative h-14 w-full">
                        <canvas id="chart_${{index}}"></canvas>
                    </div>
                `;
                container.appendChild(card);

                const ctx = document.getElementById(`chart_${{index}}`).getContext('2d');
                const labels = day.points.map(p => p.time);
                const values = day.points.map(p => p.val);

                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            data: values,
                            borderWidth: 2,
                            stepped: true,
                            fill: true,
                            tension: 0,
                            pointRadius: 2,
                            segment: {{
                                borderColor: ctx => {{
                                    const v0 = ctx.p0.parsed.y;
                                    const v1 = ctx.p1.parsed.y;
                                    if (v0 === 1 && v1 === 1) return '#10b981'; // Зеленый
                                    if (v0 === 0 && v1 === 0) return '#ef4444'; // Красный
                                    if (v0 === -1 && v1 === -1) return '#0f172a'; // Черный (нет инфы)
                                    return '#64748b';
                                }},
                                backgroundColor: ctx => {{
                                    const v0 = ctx.p0.parsed.y;
                                    const v1 = ctx.p1.parsed.y;
                                    if (v0 === 1 && v1 === 1) return 'rgba(16, 185, 129, 0.15)';
                                    if (v0 === 0 && v1 === 0) return 'rgba(239, 68, 68, 0.15)';
                                    if (v0 === -1 && v1 === -1) return 'rgba(15, 23, 42, 0.15)';
                                    return 'rgba(100, 116, 139, 0.15)';
                                }}
                            }},
                            pointBackgroundColor: values.map(v => {{
                                if (v === 1) return '#10b981';
                                if (v === 0) return '#ef4444';
                                return '#0f172a';
                            }})
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            x: {{
                                grid: {{ display: false }},
                                ticks: {{ color: '#64748b', font: {{ size: 9 }}, maxTicksLimit: 8 }}
                            }},
                            y: {{
                                min: -1.2,
                                max: 1.2,
                                ticks: {{
                                    stepSize: 1,
                                    color: '#64748b',
                                    font: {{ size: 8 }},
                                    callback: function(value) {{
                                        if (value === 1) return 'Есть';
                                        if (value === 0) return 'Нет';
                                        if (value === -1) return 'Нет инфы';
                                        return '';
                                    }}
                                }},
                                grid: {{ color: 'rgba(0, 0, 0, 0.04)' }}
                            }}
                        }},
                        plugins: {{
                            legend: {{ display: false }},
                            tooltip: {{
                                callbacks: {{
                                    title: function(context) {{
                                        return `${{day.date_str}} ${{context[0].label}}`;
                                    }},
                                    label: function(context) {{
                                        const y = context.parsed.y;
                                        if (y === 1) return ' Свет есть';
                                        if (y === 0) return ' Света нет';
                                        return ' Нет информации';
                                    }}
                                }}
                            }}
                        }}
                    }}
                }});
            }});
        </script>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/dashboard", handle_dashboard)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[WEB] Веб-сервер и дашборд запущены на порту {port}", flush=True)

async def main():
    print("[СТАРТ] Инициализация...", flush=True)
    init_db()
    await start_web_server()
    asyncio.create_task(monitor_loop())
    print("[СТАРТ] Запуск пуллинга...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())