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
    BufferedInputFile,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    WebAppInfo,
)
from aiohttp import web
import psycopg2

from chart_generator import generate_schedule_image
from dtek_checker import (
    checker_main_loop,
    claim_notification,
    get_latest_schedule_for_date,
    init_dtek_db,
    run_dtek_check_manual,
)

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")

TARGET_IP = "176.36.251.189"
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://powerbot-q183.onrender.com/dashboard")

CHECK_INTERVAL = 5
FAIL_THRESHOLD = 2
NOTIFY_COOLDOWN_SEC = 120  # Защита от частых повторных алертов при дребезге сети/перезапусках

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

last_check_time = "Еще не проводилась"
last_notify_ts = 0.0
last_notified_state = None


def get_kyiv_now():
    return datetime.datetime.now(ZoneInfo("Europe/Kiev"))


def get_kyiv_time():
    return get_kyiv_now().strftime("%Y-%m-%d %H:%M:%S")


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в переменных окружения!")
    return psycopg2.connect(DATABASE_URL)


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📊 Статус"),
                KeyboardButton(text="📊 График"),
            ],
            [
                KeyboardButton(text="📈 Веб-Дашборд 🌐", web_app=WebAppInfo(url=WEB_APP_URL))
            ],
        ],
        resize_keyboard=True,
    )


def get_logs_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ 10 минут", callback_data="logs_10"),
                InlineKeyboardButton(text="⏱ 60 минут", callback_data="logs_60"),
                InlineKeyboardButton(text="⏱ 24 часа", callback_data="logs_1440"),
            ]
        ]
    )


def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить статус (за 2 дня)", callback_data="admin_set_status")],
            [InlineKeyboardButton(text="🔄 Проверить график ДТЭК", callback_data="admin_check_dtek")],
            [InlineKeyboardButton(text="📄 Выгрузить логи", callback_data="admin_logs")],
            [InlineKeyboardButton(text="🔄 Перезагрузить бота", callback_data="admin_restart")],
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            notif_key VARCHAR(128) PRIMARY KEY,
            sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
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
    except Exception:
        return []


def log_event(status):
    now = get_kyiv_time()
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
    now = get_kyiv_time()
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
    now_kyiv = get_kyiv_now()
    target_date = (now_kyiv + datetime.timedelta(days=1 if is_tomorrow else 0)).strftime("%Y-%m-%d")
    return get_latest_schedule_for_date(target_date)


def build_dtek_text_intervals(schedule_items):
    """
    Формирует читаемые интервалы вида:
    'С 00:00 до 02:00 — света нет' / 'С 02:00 до 10:00 — свет есть'
    """
    if not schedule_items:
        return []

    slots = []  # (start_min, end_min, has_light: bool)
    for item in schedule_items:
        hour_str = str(item.get("hour", "")).strip()
        status = str(item.get("status", "ON")).strip()
        try:
            p = hour_str.split("-")
            sh = int(p[0].split(":")[0])
            eh = int(p[1].split(":")[0])
            if eh == 0 and sh > 0:
                eh = 24
        except Exception:
            continue

        s_m = sh * 60
        e_m = eh * 60
        if e_m <= s_m:
            continue

        if status == "OFF_FIRST_30":
            mid = min(s_m + 30, e_m)
            slots.append((s_m, mid, False))
            if mid < e_m:
                slots.append((mid, e_m, True))
        elif status == "OFF_SECOND_30":
            mid = max(e_m - 30, s_m)
            if s_m < mid:
                slots.append((s_m, mid, True))
            slots.append((mid, e_m, False))
        elif status == "OFF":
            slots.append((s_m, e_m, False))
        else:
            slots.append((s_m, e_m, True))

    merged = []
    for s_m, e_m, has_light in slots:
        if merged and merged[-1][2] == has_light and merged[-1][1] == s_m:
            merged[-1] = (merged[-1][0], e_m, has_light)
        else:
            merged.append((s_m, e_m, has_light))

    def fmt_m(m):
        if m >= 1440:
            return "24:00"
        return f"{m // 60:02d}:{m % 60:02d}"

    result = []
    for s_m, e_m, has_light in merged:
        label = "свет есть" if has_light else "света нет"
        result.append({
            "text": f"С {fmt_m(s_m)} до {fmt_m(e_m)} — {label}",
            "has_light": has_light,
        })
    return result


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
        delay = ping(ip, timeout=2, unit="ms")
        is_success = delay is not None and delay is not False
        if is_success:
            ms_val = round(delay, 1)
            log_ping(ms_val)
            print(f"[ПИНГ] Проверка {ip} -> Успешно | Отклик: {ms_val} ms", flush=True)
            return True
        print(f"[ПИНГ] Проверка {ip} -> Неуспешно (Нет ответа)", flush=True)
        return False
    except Exception:
        return False


async def broadcast_message(text, reply_markup=None, notif_key: str | None = None):
    if notif_key and not claim_notification(notif_key):
        print(f"[УВЕДОМЛЕНИЯ] Дубль отклонён ключом: {notif_key}", flush=True)
        return

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
    now_kyiv = get_kyiv_now()
    today_sched = get_schedule_from_db(is_tomorrow=False)
    tomorrow_sched = get_schedule_from_db(is_tomorrow=True)

    days_for_img = []
    if today_sched:
        days_for_img.append((f"Сегодня ({now_kyiv.strftime('%d.%m')})", today_sched))
    if tomorrow_sched:
        tmr_str = (now_kyiv + datetime.timedelta(days=1)).strftime("%d.%m")
        days_for_img.append((f"Завтра ({tmr_str})", tomorrow_sched))

    if not days_for_img:
        await message.answer("📅 График ещё не загружен.", reply_markup=get_main_keyboard())
        return

    img_bytes = generate_schedule_image(days_for_img)
    await message.answer_photo(
        BufferedInputFile(img_bytes, filename="schedule.png"),
        reply_markup=get_main_keyboard(),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    add_user(message.chat.id)
    await message.answer("🛠 **Панель администратора:**", parse_mode="Markdown", reply_markup=get_admin_keyboard())


@dp.callback_query(F.data == "admin_set_status")
async def cb_admin_set_status(callback: CallbackQuery):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        two_days_ago = get_kyiv_now() - datetime.timedelta(days=2)
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
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
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
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 Свет", callback_data=f"saveev_{event_id}_ONLINE"),
                    InlineKeyboardButton(text="🟡 Резерв", callback_data=f"saveev_{event_id}_RESERVE"),
                    InlineKeyboardButton(text="🔴 Нет света", callback_data=f"saveev_{event_id}_OFFLINE"),
                ],
                [InlineKeyboardButton(text="« К списку событий", callback_data="admin_set_status")],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("saveev_"))
async def cb_save_event_status(callback: CallbackQuery):
    parts = callback.data.split("_")
    event_id = int(parts[1])
    new_status = parts[2]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE events SET status = %s WHERE id = %s;", (new_status, event_id))
        updated = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if updated == 0:
            await callback.message.edit_text(f"⚠️ Событие ID `{event_id}` не найдено.", parse_mode="Markdown")
        else:
            status_names = {"ONLINE": "🟢 Свет есть", "RESERVE": "🟡 Резервное питание", "OFFLINE": "🔴 Света нет"}
            await callback.message.edit_text(
                f"✅ Статус события ID `{event_id}` успешно изменен на: **{status_names.get(new_status, new_status)}**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🛠 В админ-панель", callback_data="admin_back")]]
                ),
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
    os.execv(sys.executable, ["python"] + sys.argv)


@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    add_user(message.chat.id)
    await message.answer("🛠 Выберите период для выгрузки логов:", reply_markup=get_logs_keyboard())


@dp.callback_query(F.data.startswith("logs_"))
async def process_logs_callback(callback: CallbackQuery):
    minutes = int(callback.data.split("_")[1])
    now_kyiv = get_kyiv_now()
    threshold_time = now_kyiv - datetime.timedelta(minutes=minutes)

    filtered_logs = [item[1] for item in log_buffer if item[0] >= threshold_time]
    if not filtered_logs:
        await callback.message.answer("📭 За выбранный период логов нет.")
        await callback.answer()
        return

    logs_text = "\n".join(filtered_logs)
    file_bytes = logs_text.encode("utf-8")
    filename = f"logs_{minutes}min_{now_kyiv.strftime('%Y%m%d_%H%M%S')}.txt"

    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption="📄 Логи за выбранный период.",
    )
    await callback.answer()


# --- МОНИТОРИНГ С ЗАЩИТОЙ ОТ ПОВТОРНЫХ СМС ---
async def monitor_loop():
    global last_notify_ts, last_notified_state
    await asyncio.sleep(3)
    init_db()
    init_dtek_db()

    last_event = get_last_event()
    current_status = last_event[1] if last_event else "ONLINE"
    last_notified_state = current_status

    if last_event and last_event[1] != "ONLINE":
        try:
            outage_start_time = datetime.datetime.strptime(last_event[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Kiev"))
        except Exception:
            outage_start_time = get_kyiv_now()
    else:
        outage_start_time = None

    if not last_event:
        log_event("ONLINE")

    fails = 0
    print("[МОНИТОРИНГ] Цикл мониторинга запущен (каждые 5 сек).", flush=True)

    while True:
        ping_ok = await ping_target(TARGET_IP)
        now = get_kyiv_now()
        now_ts = now.timestamp()

        schedule = get_schedule_from_db(is_tomorrow=False)
        scheduled_off = False
        if schedule:
            for item in schedule:
                if str(item.get("hour", "")).startswith(f"{now.hour:02d}"):
                    st = item.get("status")
                    if st == "OFF":
                        scheduled_off = True
                    elif st == "OFF_FIRST_30" and now.minute < 30:
                        scheduled_off = True
                    elif st == "OFF_SECOND_30" and now.minute >= 30:
                        scheduled_off = True
                    break

        if ping_ok:
            fails = 0
            if current_status in ["OFFLINE", "RESERVE"]:
                down_duration = (now - outage_start_time).total_seconds() if outage_start_time else 60.0

                if scheduled_off or down_duration <= 300:
                    new_st = "RESERVE"
                    text = f"🟡 Возобновление питания через резерв в {now.strftime('%H:%M')}."
                else:
                    new_st = "ONLINE"
                    text = f"🟢 Свет появился в {now.strftime('%H:%M')}. Не было {format_duration(down_duration)}."

                current_status = new_st
                log_event(new_st)
                outage_start_time = None

                # Защита от спама при быстром переключении состояний
                bucket_min = int(now_ts // 120)
                notif_key = f"pwr_{new_st}_{now.strftime('%Y%m%d')}_{bucket_min}"
                if new_st != last_notified_state or (now_ts - last_notify_ts) >= NOTIFY_COOLDOWN_SEC:
                    last_notify_ts = now_ts
                    last_notified_state = new_st
                    await broadcast_message(text, reply_markup=get_main_keyboard(), notif_key=notif_key)
        else:
            fails += 1
            if fails >= FAIL_THRESHOLD and current_status in ["ONLINE", "RESERVE"]:
                fails = 0
                outage_start_time = now
                new_st = "OFFLINE"
                current_status = new_st
                log_event(new_st)

                text = f"🔴 Свет пропал в {now.strftime('%H:%M')}."
                bucket_min = int(now_ts // 120)
                notif_key = f"pwr_{new_st}_{now.strftime('%Y%m%d')}_{bucket_min}"
                if new_st != last_notified_state or (now_ts - last_notify_ts) >= NOTIFY_COOLDOWN_SEC:
                    last_notify_ts = now_ts
                    last_notified_state = new_st
                    await broadcast_message(text, reply_markup=get_main_keyboard(), notif_key=notif_key)

        await asyncio.sleep(CHECK_INTERVAL)


# --- ВЕБ-СЕРВЕР И ДАШБОРД ---
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
        cursor.execute("SELECT id, schedule_date, schedule_json FROM dtek_schedules ORDER BY id ASC;")
        dtek_rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception:
        rows = []
        ping_rows = []
        dtek_rows = []

    dtek_by_date = {}
    for _, d_date, d_json in dtek_rows:
        d_str = d_date.strftime("%Y-%m-%d") if isinstance(d_date, datetime.date) else str(d_date)
        dtek_by_date[d_str] = d_json

    now_kyiv = get_kyiv_now()
    today_date = now_kyiv.date()
    days = [(now_kyiv - datetime.timedelta(days=6 - i)) for i in range(7)]

    ru_weekdays = {
        0: "Понедельник",
        1: "Вторник",
        2: "Среда",
        3: "Четверг",
        4: "Пятница",
        5: "Суббота",
        6: "Воскресенье",
    }

    def format_time_readable(minutes):
        if minutes is None or minutes <= 0:
            return "0 мин."
        h = int(minutes // 60)
        m = int(round(minutes % 60))
        if h > 0 and m > 0:
            return f"{h} ч. {m} мин."
        if h > 0:
            return f"{h} ч."
        return f"{m} мин."

    raw_days_stats = []

    for day in days:
        day_date = day.date()
        weekday_name = ru_weekdays[day_date.weekday()]
        date_str_formatted = f"{day_date.strftime('%d.%m')} ({weekday_name})"
        date_iso_str = day_date.strftime("%Y-%m-%d")

        dtek_segments = []
        day_dtek_json = dtek_by_date.get(date_iso_str)
        dtek_text_intervals = build_dtek_text_intervals(day_dtek_json) if day_dtek_json else []

        if day_dtek_json:
            for item in day_dtek_json:
                hour_range = item["hour"]
                try:
                    start_h, end_h = map(int, hour_range.split("-"))
                    if end_h == 0 and start_h > 0:
                        end_h = 24
                    base_left = (start_h / 24.0) * 100
                    base_width = ((end_h - start_h) / 24.0) * 100

                    st = item["status"]
                    if st == "OFF_FIRST_30":
                        width_val = base_width / 2.0
                        left_val = base_left
                        is_off = True
                    elif st == "OFF_SECOND_30":
                        width_val = base_width / 2.0
                        left_val = base_left + (base_width / 2.0)
                        is_off = True
                    else:
                        width_val = base_width
                        left_val = base_left
                        is_off = (st == "OFF" or "Нет" in item.get("desc", ""))

                    bg_col = "#fca5a5" if is_off else "#86efac"
                    dtek_segments.append({
                        "left": left_val,
                        "width": width_val,
                        "color": bg_col,
                        "text": f"План ДТЭК ({item['hour']}): {item['desc']}",
                    })
                except Exception:
                    pass

        if day_date > today_date:
            raw_days_stats.append({
                "date_str": date_str_formatted,
                "segments": [],
                "dtek_segments": dtek_segments,
                "dtek_intervals": dtek_text_intervals,
                "details": [],
                "offline_mins": None,
                "online_mins": None,
                "avg_ping": None,
                "has_data": False,
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
            if ev_min > current_min and current_st is not None:
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
                    "left": left_pct,
                    "width": width_pct,
                    "color": bg_color,
                    "text": f"{st_name}: с {start_t} до {end_t} ({format_time_readable(dur)})",
                })
                details_list.append({
                    "text": f"{st_name} с {start_t} до {end_t}",
                    "dur": format_time_readable(dur),
                    "color_class": st_color,
                })
                current_min = ev_min
            current_st = st

        if current_min < end_limit_min and current_st is not None:
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
                "left": left_pct,
                "width": width_pct,
                "color": bg_color,
                "text": f"{st_name}: с {start_t} до {end_t} ({format_time_readable(dur)})",
            })
            details_list.append({
                "text": f"{st_name} с {start_t} до {end_t}",
                "dur": format_time_readable(dur),
                "color_class": st_color,
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
            "dtek_intervals": dtek_text_intervals,
            "details": details_list,
            "offline_mins": offline_mins if has_data else None,
            "online_mins": online_mins if has_data else None,
            "avg_ping": avg_ping,
            "has_data": has_data,
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
                online_symbol = '<span class="text-emerald-500 font-extrabold ml-1">↓</span>'

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
            "dtek_intervals": curr["dtek_intervals"],
            "details": curr["details"],
            "offline_str": format_time_readable(curr["offline_mins"]) if curr["has_data"] else "--//--",
            "online_str": format_time_readable(curr["online_mins"]) if curr["has_data"] else "--//--",
            "ping_str": f"{curr['avg_ping']} мс" if curr["avg_ping"] is not None else "--//--",
            "offline_symbol": offline_symbol if curr["has_data"] else "",
            "online_symbol": online_symbol if curr["has_data"] else "",
            "ping_symbol": ping_symbol if curr["has_data"] else "",
        })

    days_json = json.dumps(days_data, ensure_ascii=False)

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
        <div class="max-w-xl mx-auto space-y-4" id="cards-container"></div>

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

                let dtekIntervalsHtml = '';
                if (day.dtek_intervals && day.dtek_intervals.length > 0) {
                    day.dtek_intervals.forEach(iv => {
                        const badgeColor = iv.has_light ? 'text-emerald-600' : 'text-red-500';
                        const dot = iv.has_light ? '🟢' : '🔴';
                        dtekIntervalsHtml += `
                            <div class="flex items-center justify-between text-xs py-1 border-b border-slate-100 last:border-0">
                                <span class="font-medium text-slate-700">${dot} ${iv.text}</span>
                                <span class="font-semibold ${badgeColor}">${iv.has_light ? 'Есть' : 'Нет'}</span>
                            </div>
                        `;
                    });
                } else {
                    dtekIntervalsHtml = '<div class="text-xs text-slate-400 py-1">Нет текстового расписания ДТЭК</div>';
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

                card.innerHTML = `
                    <div class="flex justify-between items-center mb-2 cursor-pointer select-none" onclick="toggleAccordion(${index})">
                        <span class="text-sm font-bold text-slate-800">${day.date_str}</span>
                        <svg id="arrow_${index}" class="w-4 h-4 text-slate-400 transition-transform duration-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path>
                        </svg>
                    </div>
                    <div class="relative h-10 w-full bg-white border border-slate-200 rounded-xl overflow-hidden shadow-inner flex">
                        ${segmentsHtml}
                    </div>
                    <div class="flex justify-between text-xs text-slate-500 mt-1.5 px-0.5 font-semibold">
                        <span>00:00</span>
                        <span>06:00</span>
                        <span>12:00</span>
                        <span>18:00</span>
                        <span>23:59</span>
                    </div>
                    <div id="toast_${index}" class="hidden mt-2 p-2 bg-slate-800 text-white text-xs rounded-lg text-center font-medium"></div>

                    <div id="accordion_${index}" style="max-height: 0px;" class="overflow-hidden transition-all duration-300 ease-in-out">
                        <div class="mt-3 pt-3 border-t border-slate-100 space-y-3">
                            <div>
                                <span class="text-xs font-bold text-slate-500 block mb-1">📋 План от ДТЭК:</span>
                                <div class="relative h-7 w-full bg-slate-50 border border-slate-200 rounded-lg overflow-hidden flex">
                                    ${dtekSegmentsHtml}
                                </div>
                                <div class="flex justify-between text-[10px] text-slate-400 mt-1 px-0.5 font-medium">
                                    <span>00:00</span>
                                    <span>06:00</span>
                                    <span>12:00</span>
                                    <span>18:00</span>
                                    <span>23:59</span>
                                </div>
                                <div class="mt-2 bg-slate-50 border border-slate-100 rounded-xl px-3 py-1.5">
                                    ${dtekIntervalsHtml}
                                </div>
                            </div>
                            <div class="space-y-1">
                                <span class="text-xs font-bold text-slate-500 block mb-1">⏱ Хронология событий:</span>
                                ${detailsHtml}
                            </div>
                        </div>
                    </div>

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
    return web.Response(text=html_content, content_type="text/html")


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
    init_dtek_db()
    await start_web_server()

    asyncio.create_task(monitor_loop())
    asyncio.create_task(checker_main_loop())

    print("[СТАРТ] Запуск пуллинга бота...", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())