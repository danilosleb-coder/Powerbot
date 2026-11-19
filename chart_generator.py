import io
from PIL import Image, ImageDraw, ImageFont


def _get_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _intervals_from_schedule(schedule_items):
    """
    Преобразует список [{hour: '00-01', status: 'ON'|'OFF'|'OFF_FIRST_30'|'OFF_SECOND_30'}]
    в поминутные сегменты (start_min, end_min, kind) за 1440 минут суток.
    kind: 'ON' | 'OFF' | 'RESERVE'
    """
    if not schedule_items:
        return []

    segments = []
    for item in schedule_items:
        hour_str = str(item.get("hour", "")).strip()
        status = str(item.get("status", "ON")).strip()
        try:
            parts = hour_str.split("-")
            start_h = int(parts[0].split(":")[0])
            end_h = int(parts[1].split(":")[0])
            if end_h == 0 and start_h > 0:
                end_h = 24
        except Exception:
            continue

        start_m = start_h * 60
        end_m = end_h * 60
        if end_m <= start_m:
            continue

        if status == "OFF_FIRST_30":
            mid_m = min(start_m + 30, end_m)
            segments.append((start_m, mid_m, "OFF"))
            if mid_m < end_m:
                segments.append((mid_m, end_m, "ON"))
        elif status == "OFF_SECOND_30":
            mid_m = max(end_m - 30, start_m)
            if start_m < mid_m:
                segments.append((start_m, mid_m, "ON"))
            segments.append((mid_m, end_m, "OFF"))
        elif status == "OFF":
            segments.append((start_m, end_m, "OFF"))
        elif status == "RESERVE":
            segments.append((start_m, end_m, "RESERVE"))
        else:
            segments.append((start_m, end_m, "ON"))

    # Склеиваем соседние одинаковые сегменты
    merged = []
    for s_m, e_m, kind in segments:
        if merged and merged[-1][2] == kind and merged[-1][1] == s_m:
            prev_s, _, prev_k = merged[-1]
            merged[-1] = (prev_s, e_m, prev_k)
        else:
            merged.append((s_m, e_m, kind))
    return merged


def generate_schedule_image(days_schedules: list[tuple[str, list[dict]]]) -> bytes:
    """
    days_schedules: список кортежей [(label_date, schedule_items), ...] длиной 1 или 2.
    Возвращает PNG в байтах — только чистые шкалы графика на 1-2 дня без лишнего текста.
    """
    num_days = max(1, min(2, len(days_schedules)))
    days_schedules = days_schedules[:num_days]

    width = 960
    row_height = 115
    top_margin = 24
    bottom_margin = 24
    height = top_margin + bottom_margin + num_days * row_height

    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    font_label = _get_font(20)
    font_axis = _get_font(15)

    left_x = 190
    right_x = 920
    bar_w = right_x - left_x
    bar_h = 38

    color_map = {
        "ON": "#4ADE80",       # зелёный — свет есть
        "OFF": "#F87171",      # красный — света нет
        "RESERVE": "#FACC15",  # жёлтый — резерв
    }

    for idx, (label_text, items) in enumerate(days_schedules):
        y_base = top_margin + idx * row_height
        bar_y = y_base + 22

        # Подпись дня слева (например: "Сегодня (19.11)" или "Завтра (20.11)")
        draw.text((20, bar_y + 6), label_text, fill="#0F172A", font=font_label)

        # Фон шкалы
        draw.rounded_rectangle(
            [left_x, bar_y, right_x, bar_y + bar_h],
            radius=8,
            fill="#F1F5F9",
            outline="#CBD5E1",
            width=2,
        )

        # Отрисовка сегментов
        segs = _intervals_from_schedule(items)
        for s_min, e_min, kind in segs:
            x1 = left_x + int((s_min / 1440.0) * bar_w)
            x2 = left_x + int((e_min / 1440.0) * bar_w)
            if x2 > x1:
                draw.rectangle(
                    [x1, bar_y + 2, x2, bar_y + bar_h - 2],
                    fill=color_map.get(kind, "#4ADE80"),
                )

        # Часовые деления (каждый час — мелкая засечка, каждые 4 часа — подпись)
        axis_y = bar_y + bar_h + 6
        for h in range(0, 25):
            hx = left_x + int((h / 24.0) * bar_w)
            tick_h = 8 if h % 4 == 0 else 4
            draw.line([(hx, bar_y + bar_h), (hx, bar_y + bar_h + tick_h)], fill="#64748B", width=1)
            if h % 4 == 0:
                t_str = f"{h:02d}:00"
                bbox = draw.textbbox((0, 0), t_str, font=font_axis)
                tw = bbox[2] - bbox[0]
                draw.text((hx - tw // 2, axis_y), t_str, fill="#475569", font=font_axis)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()