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
from dtek_checker import checker_main_loop, run_dtek_check_manual
from aiogram.types import (
    BufferedInputFile, Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo
)
from aiohttp import web
import psycopg2

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "9285635116:AAH9tk30qN2xAcdM-ChCtr0kSWKT5XEMPik")
TARGET_IP = "176.36.251.189"
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://powerbot-q183.onrender.com/dashboard")

CHECK_INTERVAL = 5   # Пинг каждые 5 секунд
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
                KeyboardButton(text="📊 График")
            ],
            [
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

def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить последний статус", callback_data="admin_set_status")],
            [InlineKeyboardButton(text="🔄 Проверить график ДТЭК", callback_data="admin_check_dtek")],
            [InlineKeyboardButton(text="📄 Выгрузить логи", callback_data="admin_logs")],
            [InlineKeyboardButton(text="🔄 Перезагрузить бота", callback_data="admin_restart")]
        ]
    )

def get_status_override_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Свет есть", callback_data="set_ONLINE"),
                InlineKeyboardButton(text="🟡 Резерв", callback_data="set_RESERVE"),
                InlineKeyboardButton(text="🔴 Света нет", callback_data="set_OFFLINE")
            ]
        ]
    )

def init_db():
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
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            status VARCHAR(20) NOT NULL
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pings (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            latency FLOAT NOT NULL
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
    print("[БАЗА ДАННЫХ] Инициализация прошла успешно.", flush=True)

def add_user(chat_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING;", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[ОШИБКА БД] {e}", flush=True)

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
        print(f"[ОШИБКА БД] {e}", flush=True)

def log_ping(latency):
    now = datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO pings (timestamp, latency) VALUES (%s, %s);", (now, latency))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        pass

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
    except Exception:
        return None

def get_schedule_from_db(is_tomorrow=False):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        target_date = (datetime.datetime.now(ZoneInfo("Europe/Kiev")) + datetime.timedelta(days=1 if is_tomorrow else 0)).strftime("%Y-%m-%d")
        cursor.execute("SELECT schedule_json FROM dtek_schedules WHERE schedule_date = %s AND is_tomorrow = %s ORDER BY id DESC LIMIT 1;", (target_date, is_tomorrow))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return row[0]
        return None
    except Exception:
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
        delay = ping(ip, timeout=2, unit='ms')
        is_success = delay is not None and delay is not False
        if is_success:
            ms_val = round(delay, 1)
            log_ping(ms_val)
            print(f"[ПИНГ] Проверка {ip} -> Успешно | Отклик: {ms_val} ms", flush=True)
            return True
        else:
            print(f"[ПИНГ] Проверка {ip} -> Неуспешно (Нет ответа)", flush=True)
            return False
    except Exception:
        return False

async def broadcast_message(text, reply_markup=None):
    users = get_all_users()
    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[ОШИБКА] {e}", flush=True)

# --- КОМАНДЫ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.chat.id)
    await message.answer("⚡ Привет! Бот мониторинга сети успешно запущен.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 Статус")
@dp.message(Command("status"))
async def cmd_status(message: Message):
    add_user(message.chat.id)
    last = get_last_event()
    if not last:
        text = "📊 История пустая."
    else:
        t, status = last
        if status == "ONLINE":
            st_text = "🟢 Свет есть"
        elif status == "RESERVE":
            st_text = "🟡 Резервное питание"
        else:
            st_text = "🔴 Света нет"
        text = f"Текущий статус: {st_text}\nСобытие с: {t}\nПоследняя проверка: {last_check_time}"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 График")
async def cmd_graph_btn(message: Message):
    add_user(message.chat.id)
    today_sched = get_schedule_from_db(is_tomorrow=False)
    tomorrow_sched = get_schedule_from_db(is_tomorrow=True)
    
    if not today_sched and not tomorrow_sched:
        await message.answer("📅 Плановый график еще не загружен из ДТЭК.")
        return
    
    msg = ""
    if today_sched:
        today_str = datetime.datetime.now(ZoneInfo("Europe/Kiev")).strftime("%d.%m.%Y")
        
        # Генерируем визуальную полоску из эмодзи для Телеграма
        bar = ""
        for item in today_sched:
            st = item['status']
            if st == 'ON':
                bar += "🟩"
            elif st in ['OFF_FIRST_30', 'OFF_SECOND_30']:
                bar += "🟧"
            else:
                bar += "🟥"
                
        msg += f"📋 **График ДТЭК на сегодня ({today_str}):**\n{bar}\n00:   06:   12:   18:   24\n\n"
        for item in today_sched:
            msg += f"`{item['hour']}` | {item['desc']}\n"
        msg += "\n"

    if tomorrow_sched:
        tomorrow_str = (datetime.datetime.now(ZoneInfo("Europe/Kiev")) + datetime.timedelta(days=1)).strftime("%d.%m.%Y")
        
        bar_tom = ""
        for item in tomorrow_sched:
            st = item['status']
            if st == 'ON':
                bar_tom += "🟩"
            elif st in ['OFF_FIRST_30', 'OFF_SECOND_30']:
                bar_tom += "🟧"
            else:
                bar_tom += "🟥"
                
        msg += f"📅 **График ДТЭК на завтра ({tomorrow_str}):**\n{bar_tom}\n00:   06:   12:   18:   24\n\n"
        for item in tomorrow_sched:
            msg += f"`{item['hour']}` | {item['desc']}\n"

    await message.answer(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    add_user(message.chat.id)
    await message.answer("🛠 **Панель администратора:**", parse_mode="Markdown", reply_markup=get_admin_keyboard())

# --- АДМИН-МЕНЮ И ИЗМЕНЕНИЕ ХРОНОЛОГИИ ЗА 2 ДНЯ ---

@dp.callback_query(F.data == "admin_set_status")
async def cb_admin_set_status(callback: CallbackQuery):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Берем события за последние 2 дня (48 часов)
        two_days_ago = datetime.datetime.now(ZoneInfo("Europe/Kiev")) - datetime.timedelta(days=2)
        cursor.execute("SELECT id, timestamp, status FROM events WHERE timestamp >= %s ORDER BY id DESC LIMIT 10;", (two_days_ago,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            await callback.message.edit_text("📭 За последние 2 дня нет записанных событий в базе.")
            await callback.answer()
            return

        keyboard_buttons = []
        status_emojis = {"ONLINE": "🟢", "RESERVE": "🟡", "OFFLINE": "🔴"}

        for r_id, t, st in rows:
            t_str = t.strftime("%d.%m %H:%M") if isinstance(t, datetime.datetime) else str(t)[5:16]
            emoji = status_emojis.get(st, "⚪")
            btn_text = f"{emoji} [{t_str}] {st}"
            keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"editevent_{r_id}")])

        keyboard_buttons.append([InlineKeyboardButton(text="« Назад в админку", callback_data="admin_back")])
        
        await callback.message.edit_text(
            "🛠 **Выберите событие для изменения за последние 2 дня:**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        )
        await callback.answer()
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки событий: {e}")
        await callback.answer()

@dp.callback_query(F.data.startswith("editevent_"))
async def cb_select_event_to_edit(callback: CallbackQuery):
    event_id = callback.data.split("_")[1]
    await callback.message.edit_text(
        f"🎯 Выбрано событие ID: `{event_id}`.\nКакой статус установить для этого периода?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Свет", callback_data=f"saveev_{event_id}_ONLINE"),
                InlineKeyboardButton(text="🟡 Резерв", callback_data=f"saveev_{event_id}_RESERVE"),
                InlineKeyboardButton(text="🔴 Нет света", callback_data=f"saveev_{event_id}_OFFLINE")
            ],
            [InlineKeyboardButton(text="« К списку событий", callback_data="admin_set_status")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("saveev_"))
async def cb_save_event_status(callback: CallbackQuery):
    parts = callback.data.split("_")
    event_id = parts[1]
    new_status = parts[2]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE events SET status = %s WHERE id = %s;", (new_status, event_id))
        conn.commit()
        cursor.close()
        conn.close()

        status_names = {"ONLINE": "🟢 Свет есть", "RESERVE": "🟡 Резервное питание", "OFFLINE": "🔴 Света нет"}
        await callback.message.edit_text(
            f"✅ Статус события ID `{event_id}` успешно изменен на: **{status_names.get(new_status, new_status)}**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_back")]
            ])
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка обновления в базе: {e}")
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery):
    await callback.message.edit_text("🛠 **Панель администратора:**", parse_mode="Markdown", reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_check_dtek")
async def cb_admin_check(callback: CallbackQuery):
    await callback.answer("⏳ Запускаю проверку графика ДТЭК...")
    res = await run_dtek_check_manual()
    await callback.message.answer(res)

@dp.callback_query(F.data == "admin_logs")
async def cb_admin_logs(callback: CallbackQuery):
    await callback.message.answer("🛠 Выберите период для выгрузки логов:", reply_markup=get_logs_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_restart")
async def cb_admin_restart(callback: CallbackQuery):
    await callback.message.answer("🔄 Перезапускаю бота...")
    await callback.answer()
    os.execv(sys.executable, ['python'] + sys.argv)

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
        await callback.message.answer(f"📭 За выбранный период логов нет.")
        await callback.answer()
        return

    logs_text = "\n".join(filtered_logs)
    file_bytes = logs_text.encode("utf-8")
    filename = f"logs_{minutes}min_{now_kyiv.strftime('%Y%m%d_%H%M%S')}.txt"

    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📄 Логи за выбранный период."
    )
    await callback.answer()
    
# --- МОНИТОРИНГ С ЛОГИКОЙ РЕЗЕРВНОГО ПИТАНИЯ ---
async def monitor_loop():
    await asyncio.sleep(3)
    last_event = get_last_event()
    current_status = last_event[1] if last_event else "ONLINE"
    
    # Исправление: корректное восстановление outage_start_time с таймзоной при перезапуске
    if last_event and last_event[1] != "ONLINE":
        try:
            outage_start_time = datetime.datetime.strptime(last_event[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Kiev"))
        except Exception:
            outage_start_time = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
    else:
        outage_start_time = None

    if not last_event:
        init_db()
        log_event("ONLINE")

    fails = 0
    print("[МОНИТОРИНГ] Цикл мониторинга запущен (каждые 5 сек).", flush=True)

    while True:
        ping_ok = await ping_target(TARGET_IP)
        now = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
        
        # Сверяемся с планом ДТЭК на текущий час с учетом получасовых интервалов
        schedule = get_schedule_from_db(is_tomorrow=False)
        scheduled_off = False
        if schedule:
            for item in schedule:
                if item['hour'].startswith(f"{now.hour:02d}"):
                    st = item['status']
                    if st == 'OFF':
                        scheduled_off = True
                    elif st == 'OFF_FIRST_30' and now.minute < 30:
                        scheduled_off = True
                    elif st == 'OFF_SECOND_30' and now.minute >= 30:
                        scheduled_off = True
                    break

        if ping_ok:
            fails = 0
            if current_status in ["OFFLINE", "RESERVE"]:
                if outage_start_time:
                    down_duration = (now - outage_start_time).total_seconds()
                else:
                    down_duration = 60

                if scheduled_off or down_duration <= 300:
                    new_st = "RESERVE"
                    text = f"🟡 Возобновление питания через резерв в {now.strftime('%H:%M')}."
                else:
                    new_st = "ONLINE"
                    text = f"🟢 Свет появился в {now.strftime('%H:%M')}. Не было {format_duration(down_duration)}."

                current_status = new_st
                log_event(new_st)
                outage_start_time = None
                await broadcast_message(text, reply_markup=get_main_keyboard())
        else:
            fails += 1
            if fails >= FAIL_THRESHOLD and current_status in ["ONLINE", "RESERVE"]:
                fails = 0
                outage_start_time = now
                
                new_st = "OFFLINE"
                current_status = new_st
                log_event(new_st)
                
                text = f"🔴 Свет пропал в {now.strftime('%H:%M')}."
                await broadcast_message(text, reply_markup=get_main_keyboard())

        await asyncio.sleep(CHECK_INTERVAL)

# --- ВЕБ-СЕРВЕР И ДАШБОРД С ПЛАНОВЫМ ГРАФИКОМ ДТЭК ---
async def handle_root(request):
    return web.Response(text="Power Bot Web Service is running!")

async def handle_dashboard(request):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, status FROM events ORDER BY timestamp ASC;")
        rows = cursor.fetchall()
        cursor.execute("SELECT timestamp, latency FROM pings ORDER BY timestamp ASC;")
        ping_rows = cursor.fetchall()
        cursor.execute("SELECT schedule_date, schedule_json FROM dtek_schedules WHERE is_tomorrow = FALSE;")
        dtek_rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        rows = []
        ping_rows = []
        dtek_rows = []

    dtek_by_date = {}
    for d_date, d_json in dtek_rows:
        d_str = d_date.strftime("%Y-%m-%d") if isinstance(d_date, datetime.date) else str(d_date)
        dtek_by_date[d_str] = d_json

    now_kyiv = datetime.datetime.now(ZoneInfo("Europe/Kiev"))
    today_date = now_kyiv.date()
    days = [(now_kyiv - datetime.timedelta(days=6 - i)) for i in range(7)]
    
    ru_weekdays = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота', 6: 'Воскресенье'}

    def format_time_readable(minutes):
        if minutes is None or minutes <= 0:
            return "0 мин."
        h = int(minutes // 60)
        m = int(round(minutes % 60))
        if h > 0 and m > 0:
            return f"{h} ч. {m} мин."
        elif h > 0:
            return f"{h} ч."
        return f"{m} мин."

    raw_days_stats = []

    for day in days:
        day_date = day.date()
        weekday_name = ru_weekdays[day_date.weekday()]
        date_str_formatted = f"{day_date.strftime('%d.%m')} ({weekday_name})"
        date_iso_str = day_date.strftime("%Y-%m-%d")

        # Корректная отрисовка плановых сегментов с учетом получасовых интервалов ДТЭК
        dtek_segments = []
        day_dtek_json = dtek_by_date.get(date_iso_str)
        if day_dtek_json:
            for item in day_dtek_json:
                hour_range = item['hour'] # например "00-01"
                try:
                    start_h, end_h = map(int, hour_range.split('-'))
                    base_left = (start_h / 24.0) * 100
                    base_width = ((end_h - start_h) / 24.0) * 100
                    
                    st = item['status']
                    if st == 'OFF_FIRST_30':
                        width_val = base_width / 2.0
                        left_val = base_left
                        is_off = True
                    elif st == 'OFF_SECOND_30':
                        width_val = base_width / 2.0
                        left_val = base_left + (base_width / 2.0)
                        is_off = True
                    else:
                        width_val = base_width
                        left_val = base_left
                        is_off = (st == 'OFF' or 'Нет' in item['desc'])

                    bg_col = "#fca5a5" if is_off else "#86efac"
                    dtek_segments.append({
                        "left": left_val,
                        "width": width_val,
                        "color": bg_col,
                        "text": f"План ДТЭК ({item['hour']}): {item['desc']}"
                    })
                except Exception:
                    pass

        if day_date > today_date:
            raw_days_stats.append({
                "date_str": date_str_formatted,
                "segments": [],
                "dtek_segments": dtek_segments,
                "details": [],
                "offline_mins": None,
                "online_mins": None,
                "avg_ping": None,
                "has_data": False
            })
            continue

        day_start = datetime.datetime.combine(day_date, datetime.time.min, tzinfo=ZoneInfo("Europe/Kiev"))
        day_end = datetime.datetime.combine(day_date, datetime.time.max, tzinfo=ZoneInfo("Europe/Kiev"))
        
        end_limit_min = (now_kyiv.hour * 60 + now_kyiv.minute + now_kyiv.second / 60.0) if day_date == today_date else 1440.0

        past_events = [r for r in rows if (r[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if r[0].tzinfo is None else r[0]) < day_start]
        current_st = past_events[-1][1] if past_events else "ONLINE"
        
        day_events = []
        for r in rows:
            ev_dt = r[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if r[0].tzinfo is None else r[0]
            if day_start <= ev_dt <= day_end:
                ev_min = ev_dt.hour * 60 + ev_dt.minute + ev_dt.second / 60.0
                if ev_min <= end_limit_min:
                    day_events.append((ev_min, r[1]))

        current_min = 0.0
        css_segments = []
        details_list = []
        offline_mins = 0.0
        online_mins = 0.0
        has_data = (len(day_events) > 0) or (current_st is not None)

        def min_to_str(m):
            hh = int(m // 60)
            mm = int(m % 60)
            return f"{hh:02d}:{mm:02d}"

        for ev_min, st in day_events:
            if ev_min > current_min:
                if current_st is not None:
                    dur = ev_min - current_min
                    if current_st == "ONLINE":
                        online_mins += dur
                        st_name = "Свет был"
                        st_color = "text-emerald-600"
                    elif current_st == "RESERVE":
                        online_mins += dur
                        st_name = "Резервное питание"
                        st_color = "text-amber-500"
                    else:
                        offline_mins += dur
                        st_name = "Света не было"
                        st_color = "text-red-500"
                    
                    width_pct = (dur / 1440.0) * 100
                    left_pct = (current_min / 1440.0) * 100
                    bg_color = "#90f59a" if current_st == "ONLINE" else ("#fde047" if current_st == "RESERVE" else "#f07a7a")
                    
                    start_t = min_to_str(current_min)
                    end_t = min_to_str(ev_min)
                    
                    css_segments.append({
                        "left": left_pct, "width": width_pct, "color": bg_color,
                        "text": f"{st_name}: с {start_t} до {end_t} ({format_time_readable(dur)})"
                    })
                    details_list.append({
                        "text": f"{st_name} с {start_t} до {end_t}",
                        "dur": format_time_readable(dur),
                        "color_class": st_color
                    })
                current_min = ev_min
            current_st = st
            
        if current_min < end_limit_min:
            if current_st is not None:
                dur = end_limit_min - current_min
                if current_st == "ONLINE":
                    online_mins += dur
                    st_name = "Свет был"
                    st_color = "text-emerald-600"
                elif current_st == "RESERVE":
                    online_mins += dur
                    st_name = "Резервное питание"
                    st_color = "text-amber-500"
                else:
                    offline_mins += dur
                    st_name = "Света не было"
                    st_color = "text-red-500"

                width_pct = (dur / 1440.0) * 100
                left_pct = (current_min / 1440.0) * 100
                bg_color = "#90f59a" if current_st == "ONLINE" else ("#fde047" if current_st == "RESERVE" else "#f07a7a")
                
                start_t = min_to_str(current_min)
                end_t = min_to_str(end_limit_min)
                
                css_segments.append({
                    "left": left_pct, "width": width_pct, "color": bg_color,
                    "text": f"{st_name}: с {start_t} до {end_t} ({format_time_readable(dur)})"
                })
                details_list.append({
                    "text": f"{st_name} с {start_t} до {end_t}",
                    "dur": format_time_readable(dur),
                    "color_class": st_color
                })

        day_pings = []
        for pr in ping_rows:
            p_dt = pr[0].replace(tzinfo=ZoneInfo("Europe/Kiev")) if pr[0].tzinfo is None else pr[0]
            if day_start <= p_dt <= day_end:
                day_pings.append(pr[1])

        avg_ping = round(sum(day_pings) / len(day_pings)) if day_pings else None

        raw_days_stats.append({
            "date_str": date_str_formatted,
            "segments": css_segments,
            "dtek_segments": dtek_segments,
            "details": details_list,
            "offline_mins": offline_mins if has_data else None,
            "online_mins": online_mins if has_data else None,
            "avg_ping": avg_ping,
            "has_data": has_data
        })

    days_data = []
    for i, curr in enumerate(raw_days_stats):
        prev = raw_days_stats[i - 1] if i > 0 else None

        offline_symbol = '<span class="text-slate-400 font-bold ml-1">—</span>'
        if curr["has_data"] and prev and prev["has_data"] and curr["offline_mins"] is not None and prev["offline_mins"] is not None:
            if curr["offline_mins"] > prev["offline_mins"]:
                offline_symbol = '<span class="text-red-500 font-extrabold ml-1">↑</span>'
            elif curr["offline_mins"] < prev["offline_mins"]:
                offline_symbol = '<span class="text-emerald-500 font-extrabold ml-1">↓</span>'

        online_symbol = '<span class="text-slate-400 font-bold ml-1">—</span>'
        if curr["has_data"] and prev and prev["has_data"] and curr["online_mins"] is not None and prev["online_mins"] is not None:
            if curr["online_mins"] > prev["online_mins"]:
                online_symbol = '<span class="text-emerald-500 font-extrabold ml-1">↑</span>'
            elif curr["online_mins"] < prev["online_mins"]:
                online_symbol = '<span class="text-red-500 font-extrabold ml-1">↓</span>'

        ping_symbol = '<span class="text-slate-400 font-bold ml-1">—</span>'
        if curr["has_data"] and prev and prev["has_data"] and curr["avg_ping"] is not None and prev["avg_ping"] is not None:
            if curr["avg_ping"] > prev["avg_ping"]:
                ping_symbol = '<span class="text-red-500 font-extrabold ml-1">↑</span>'
            elif curr["avg_ping"] < prev["avg_ping"]:
                ping_symbol = '<span class="text-emerald-500 font-extrabold ml-1">↓</span>'

        days_data.append({
            "date_str": curr["date_str"],
            "segments": curr["segments"],
            "dtek_segments": curr["dtek_segments"],
            "details": curr["details"],
            "offline_str": format_time_readable(curr["offline_mins"]) if curr["has_data"] else "--//--",
            "online_str": format_time_readable(curr["online_mins"]) if curr["has_data"] else "--//--",
            "ping_str": f"{curr['avg_ping']} мс" if curr["avg_ping"] is not None else "--//--",
            "offline_symbol": offline_symbol if curr["has_data"] else "",
            "online_symbol": online_symbol if curr["has_data"] else "",
            "ping_symbol": ping_symbol if curr["has_data"] else ""
        })

    days_json = json.dumps(days_data)

    html_template = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Powerbot Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
    </head>
    <body class="bg-slate-100 text-slate-800 font-sans antialiased p-4 min-h-screen">
        <div class="max-w-xl mx-auto space-y-4" id="cards-container">
            <!-- Карточки дней -->
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();

            const daysData = /*DAYS_JSON_PLACEHOLDER*/;
            const container = document.getElementById('cards-container');

            daysData.forEach((day, index) => {
                const card = document.createElement('div');
                card.className = 'bg-white border border-slate-200 rounded-2xl p-4 shadow-sm';
                
                let segmentsHtml = '';
                day.segments.forEach(seg => {
                    segmentsHtml += `<div class="absolute h-full top-0 transition-opacity active:opacity-75 cursor-pointer" style="left: ${seg.left}%; width: ${seg.width}%; background-color: ${seg.color};" onclick="showToast(event, ${index}, '${seg.text}')"></div>`;
                });

                let dtekSegmentsHtml = '';
                if (day.dtek_segments && day.dtek_segments.length > 0) {
                    day.dtek_segments.forEach(seg => {
                        dtekSegmentsHtml += `<div class="absolute h-full top-0 opacity-80" style="left: ${seg.left}%; width: ${seg.width}%; background-color: ${seg.color};" title="${seg.text}"></div>`;
                    });
                } else {
                    dtekSegmentsHtml = '<div class="flex items-center justify-center h-full text-xs text-slate-400">Нет данных графика ДТЭК</div>';
                }

                let detailsHtml = '';
                if (day.details.length > 0) {
                    day.details.forEach(det => {
                        detailsHtml += `
                            <div class="flex justify-between items-center text-xs py-1.5 border-b border-slate-100 last:border-0">
                                <span class="font-medium text-slate-700">${det.text}</span>
                                <span class="font-bold ${det.color_class}">${det.dur}</span>
                            </div>
                        `;
                    });
                } else {
                    detailsHtml = '<div class="text-xs text-slate-400 text-center py-2">Нет подробных данных</div>';
                }

                <div class="flex justify-between items-center mb-2 cursor-pointer select-none" onclick="toggleAccordion(${index})">
                        <span class="text-sm font-bold text-slate-800">${day.date_str}</span>
                        <svg id="arrow_${index}" class="w-4 h-4 text-slate-400 transition-transform duration-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path>
                        </svg>
                    </div>
                    <!-- Фактическая шкала времени -->
                    <div class="relative h-10 w-full bg-white border border-slate-200 rounded-xl overflow-hidden shadow-inner flex">
                        ${segmentsHtml}
                    </div>
                    <!-- Метки времени для факта -->
                    <div class="flex justify-between text-xs text-slate-500 mt-1.5 px-0.5 font-semibold">
                        <span>00:00</span>
                        <span>06:00</span>
                        <span>12:00</span>
                        <span>18:00</span>
                        <span>23:59</span>
                    </div>
                    <div id="toast_${index}" class="hidden mt-2 p-2 bg-slate-800 text-white text-xs rounded-lg text-center font-medium"></div>

                    <!-- Выпадающий аккордеон с плановым графиком ДТЭК и часами -->
                    <div id="accordion_${index}" style="max-height: 0px;" class="overflow-hidden transition-all duration-300 ease-in-out">
                        <div class="mt-3 pt-3 border-t border-slate-100 space-y-3">
                            <div>
                                <span class="text-xs font-bold text-slate-500 block mb-1">📋 План от ДТЭК:</span>
                                <div class="relative h-7 w-full bg-slate-50 border border-slate-200 rounded-lg overflow-hidden flex">
                                    ${dtekSegmentsHtml}
                                </div>
                                <!-- Метки времени для плана ДТЭК -->
                                <div class="flex justify-between text-[10px] text-slate-400 mt-1 px-0.5 font-medium">
                                    <span>00:00</span>
                                    <span>06:00</span>
                                    <span>12:00</span>
                                    <span>18:00</span>
                                    <span>23:59</span>
                                </div>
                            </div>
                            <div class="space-y-1">
                                <span class="text-xs font-bold text-slate-500 block mb-1">⏱ Хронология событий:</span>
                                ${detailsHtml}
                            </div>
                        </div>
                    </div>

                    <!-- Статистика -->
                    <div class="mt-3 pt-3 border-t border-slate-100 grid grid-cols-3 gap-2 text-center">
                        <div class="bg-slate-50 p-2 rounded-xl">
                            <span class="text-[10px] text-slate-400 block font-medium">Света не было</span>
                            <span class="text-xs font-bold text-slate-700 mt-0.5 inline-flex items-center justify-center">${day.offline_str}${day.offline_symbol}</span>
                        </div>
                        <div class="bg-slate-50 p-2 rounded-xl">
                            <span class="text-[10px] text-slate-400 block font-medium">Свет был</span>
                            <span class="text-xs font-bold text-slate-700 mt-0.5 inline-flex items-center justify-center">${day.online_str}${day.online_symbol}</span>
                        </div>
                        <div class="bg-slate-50 p-2 rounded-xl">
                            <span class="text-[10px] text-slate-400 block font-medium">Средний отклик</span>
                            <span class="text-xs font-bold text-slate-700 mt-0.5 inline-flex items-center justify-center">${day.ping_str}${day.ping_symbol}</span>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });

            function toggleAccordion(index) {
                const acc = document.getElementById(`accordion_${index}`);
                const arrow = document.getElementById(`arrow_${index}`);
                if (acc.style.maxHeight && acc.style.maxHeight !== '0px') {
                    acc.style.maxHeight = '0px';
                    arrow.classList.remove('rotate-180');
                } else {
                    acc.style.maxHeight = acc.scrollHeight + 'px';
                    arrow.classList.add('rotate-180');
                }
            }

            function showToast(event, index, text) {
                event.stopPropagation();
                document.querySelectorAll('[id^="toast_"]').forEach(t => t.classList.add('hidden'));
                const toast = document.getElementById(`toast_${index}`);
                toast.innerText = text;
                toast.classList.remove('hidden');
                setTimeout(() => { toast.classList.add('hidden'); }, 4000);
            }
        </script>
    </body>
    </html>
    """
    
    html_content = html_template.replace("/*DAYS_JSON_PLACEHOLDER*/", days_json)
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

    asyncio.create_task(monitor_loop())       # Пинги сети каждые 5 сек
    asyncio.create_task(checker_main_loop())  # ДТЭК чекер

    print("[СТАРТ] Запуск пуллинга бота...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())