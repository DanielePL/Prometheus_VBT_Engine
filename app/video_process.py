import cv2
import numpy as np
from scipy.interpolate import interp1d
from ultralytics import YOLO
from typing import List, Tuple, Dict
from math import sqrt, hypot

# ===== модель =====
model = YOLO('app/models/best.pt')

# ===== константы/гиперпараметры =====
BARBELL_WEIGHT_KG = 60
ONE_RM = 90
PX_TO_METER = 1000

BAR_CLASS_NAME = 'barbell'
CONF_THR = 0.25
IOU_THR  = 0.50
IMG_SZ   = 640

EMA_ALPHA     = 0.25       # сглаживание центра
MAX_JUMP_FRAC = 0.035      # доля диагонали кадра: > порога — игнор «скачок»

# Толщина/радиус линии (делаем ощутимо толще)
LINE_PERC   = 0.01        # 0.9% от средн. размера кадра
DOT_PERC    = 0    # 1.4% от средн. размера кадра
LINE_MIN_PX = 4
DOT_MIN_PX  = 6
COLOR_LINE  = (0, 165, 255)
COLOR_DOT   = (0, 255, 0)

# ========= метрики =========
def calculate_reps(y_positions: List[int]) -> int:
    reps = 0
    for i in range(1, len(y_positions) - 1):
        if y_positions[i - 1] > y_positions[i] < y_positions[i + 1]:
            reps += 1
    return max(reps, 1)

def calculate_bar_path_length(points: List[Tuple[int, int]]) -> float:
    # фикс: оператор возведения в степень
    length_px = sum(
        sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)
        for p1, p2 in zip(points[:-1], points[1:])
    )
    return length_px / PX_TO_METER

def calculate_avg_velocity(path_m: float, time_s: float) -> float:
    return path_m / time_s if time_s else 0.0

def calculate_load_percent(weight: float, one_rm: float) -> float:
    return round((weight / one_rm) * 100, 2)

def calculate_tut(frames_count: int, fps: float) -> float:
    return round(frames_count / fps, 2)

def calculate_accuracy(points: List[Tuple[int, int]], frame_width: int) -> float:
    if len(points) < 2:
        return 100.0

    xs = np.asarray([p[0] for p in points], dtype=float)
    ys = np.asarray([p[1] for p in points], dtype=float)

    x_start, y_start = float(xs[0]), float(ys[0])
    x_end, y_end     = float(xs[-1]), float(ys[-1])

    if y_end == y_start:
        return 100.0

    ideal_line = interp1d(
        [y_start, y_end],
        [x_start, x_end],
        kind="linear",
        bounds_error=False,
        fill_value="extrapolate",
        assume_sorted=False,
    )

    y_min, y_max = (min(y_start, y_end), max(y_start, y_end))
    ys_safe = np.clip(ys, y_min, y_max)

    ideal_xs = ideal_line(ys_safe).astype(float)
    deviation = np.abs(ideal_xs - xs)
    avg_dev = float(np.mean(deviation))

    if frame_width <= 0:
        return 100.0
    acc = 100.0 - (avg_dev / float(frame_width) * 100.0)
    return round(max(0.0, min(100.0, acc)), 2)

def detect_fatigue(velocities: List[float], accuracies: List[float]) -> str:
    if len(velocities) < 3:
        return "Without alert"
    v1, v2, v3 = velocities[0], velocities[len(velocities)//2], velocities[-1]
    a1, a2, a3 = accuracies[0], accuracies[len(accuracies)//2], accuracies[-1]
    v_drop = (v1 - v3) / v1 if v1 else 0
    a_drop = (a1 - a3) / a1 if a1 else 0
    if v_drop > 0.1 or a_drop > 0.1:
        return "With alert"
    return "Without alert"

def calculate_force(weight: float, velocity: float, time_per_rep: float) -> float:
    acceleration = velocity / time_per_rep if time_per_rep else 0
    return round(weight * acceleration, 3)

# ========= выбор одной штанги на кадр =========
def pick_bar_box(results, last_center: Tuple[int, int] | None) -> Dict | None:
    """
    Возвращает словарь:
      {'center': (cx, cy), 'conf': float, 'xyxy': (x1, y1, x2, y2)}
    или None, если подходящих нет.
    """
    boxes = results[0].boxes
    if boxes is None or len(boxes) == 0:
        return None

    names = results[0].names  # id -> name
    candidates = []
    for i in range(len(boxes)):
        cls_id = int(boxes.cls[i].item())
        name = names.get(cls_id, str(cls_id))
        if name != BAR_CLASS_NAME:
            continue
        conf = float(boxes.conf[i].item())
        if conf < CONF_THR:
            continue
        x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy().astype(int).tolist()
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        candidates.append({
            'center': (cx, cy),
            'conf': conf,
            'xyxy': (x1, y1, x2, y2)
        })

    if not candidates:
        return None

    # первый раз — самая уверенная
    if last_center is None:
        candidates.sort(key=lambda d: d['conf'], reverse=True)
        return candidates[0]

    # иначе — ближайшая к прошлому центру
    best = None
    best_d = 1e9
    for c in candidates:
        cx, cy = c['center']
        d = hypot(cx - last_center[0], cy - last_center[1])
        if d < best_d:
            best_d = d
            best = c
    return best

# ========= основной процесс =========
def process_video(video_path: str, output_path: str) -> Tuple[Dict, str]:
    cap = cv2.VideoCapture(video_path)
    frame_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    start_frame = 1                 # не ждём 1 сек — начинаем сразу
    stop_frame  = total_frames      # до конца файла

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (frame_width, frame_height))

    frames: List[np.ndarray] = []
    frame_idx = 0

    # трекинг одной штанги
    trajectory_points: Dict[int, Tuple[int, int]] = {}
    last_center: Tuple[int, int] | None = None

    # порог «скачка» в пикселях
    diag = (frame_width**2 + frame_height**2) ** 0.5
    max_jump_px = MAX_JUMP_FRAC * diag

    # сразу посчитаем толщины
    avg_size = (frame_width + frame_height) / 2.0
    thickness = max(int(avg_size * LINE_PERC), LINE_MIN_PX)
    radius    = max(int(avg_size * DOT_PERC),  DOT_MIN_PX)

    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        frames.append(frame.copy())

        if frame_idx < start_frame or frame_idx > stop_frame:
            continue

        # YOLO — без ресайза, в родном размере кадра
        results = model.predict(
            frame,
            verbose=False,
            conf=CONF_THR,
            iou=IOU_THR,
            imgsz=IMG_SZ,
            classes=None
        )

        chosen = pick_bar_box(results, last_center)
        if chosen is None:
            # детекции нет — продлеваем последнюю точку, чтобы линия не рвалась
            if last_center is not None:
                trajectory_points[frame_idx] = last_center
            continue

        cx, cy = chosen['center']
        if last_center is not None:
            # анти-скачок
            if hypot(cx - last_center[0], cy - last_center[1]) > max_jump_px:
                trajectory_points[frame_idx] = last_center
                continue
            # EMA сглаживание
            cx = int(EMA_ALPHA * cx + (1 - EMA_ALPHA) * last_center[0])
            cy = int(EMA_ALPHA * cy + (1 - EMA_ALPHA) * last_center[1])

        last_center = (cx, cy)
        trajectory_points[frame_idx] = last_center

    cap.release()

    # === постобработка траектории (интерполяция, чтобы не было провалов) ===
    frames_found = np.array(sorted(trajectory_points.keys()))
    if frames_found.size < 2:
        # мало точек — вернём «пустые» метрики, но файл запишем как есть
        for img in frames:
            out.write(img)
        out.release()
        result = {
            "trajectory": [],
            "reps": 0,
            "velocity": 0.0,
            "load_percent": calculate_load_percent(BARBELL_WEIGHT_KG, ONE_RM),
            "1rm": ONE_RM,
            "tut": 0.0,
            "bar_path": "0m 0cm",
            "bar_path_accuracy_percent": 100.0,
            "fatigue": "Without alert",
            "force_per_rep": 0.0
        }
        return result, output_path

    pts_arr = np.array([trajectory_points[f] for f in frames_found], dtype=float)
    pts_arr[:, 0] = np.clip(pts_arr[:, 0], 0, frame_width  - 1)
    pts_arr[:, 1] = np.clip(pts_arr[:, 1], 0, frame_height - 1)

    interp_x = interp1d(
        frames_found, pts_arr[:, 0],
        kind='linear', bounds_error=False,
        fill_value=(pts_arr[0, 0], pts_arr[-1, 0]), assume_sorted=True
    )
    interp_y = interp1d(
        frames_found, pts_arr[:, 1],
        kind='linear', bounds_error=False,
        fill_value=(pts_arr[0, 1], pts_arr[-1, 1]), assume_sorted=True
    )

    first_draw_f = max(start_frame, int(frames_found[0]))
    last_draw_f  = min(stop_frame,  int(frames_found[-1]))

    # полный интерполированный маршрут
    interpolated: Dict[int, Tuple[int, int]] = {
        f: (int(interp_x(f)), int(interp_y(f)))
        for f in range(first_draw_f, last_draw_f + 1)
    }
    full_traj: List[Tuple[int, int]] = [interpolated[f] for f in range(first_draw_f, last_draw_f + 1)]

    # «замороженная» линия — рисуем её после обработки всегда
    frozen_traj = full_traj[:]

    # === отрисовка: линия не исчезает ===
    for i, frame in enumerate(frames):
        idx = i + 1
        img = frame.copy()

        if idx < first_draw_f:
            out.write(img)
            continue

        if idx <= last_draw_f:
            # растущая линия в процессе
            pts = [interpolated[f] for f in range(first_draw_f, idx + 1) if f in interpolated]
        else:
            # после завершения — всегда полная замороженная линия
            pts = frozen_traj

        if pts:
            pts_np = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
            # базовая толстая линия
            cv2.polylines(img, [pts_np], False, COLOR_LINE, thickness, lineType=cv2.LINE_AA)
            # лёгкий «глоу» (опционально) — дублирующий слой толще и полупрозрачный
            glow = img.copy()
            cv2.polylines(glow, [pts_np], False, COLOR_LINE, thickness + 4, lineType=cv2.LINE_AA)
            img = cv2.addWeighted(glow, 0.35, img, 0.65, 0)

            # текущая точка
            cx, cy = pts[-1]
            cv2.circle(img, (cx, cy), radius, COLOR_DOT, -1, lineType=cv2.LINE_AA)

        out.write(img)

    out.release()

    # === метрики ===
    y_positions = [p[1] for p in full_traj]
    reps        = calculate_reps(y_positions)
    path_m      = calculate_bar_path_length(full_traj)
    duration_s  = len(full_traj) / fps if fps else 0.0
    velocity    = calculate_avg_velocity(path_m, duration_s)
    load_pct    = calculate_load_percent(BARBELL_WEIGHT_KG, ONE_RM)
    tut         = calculate_tut(len(full_traj), fps if fps else 1)
    accuracy    = calculate_accuracy(full_traj, frame_width)
    fatigue     = detect_fatigue([velocity] * max(reps, 1), [accuracy] * max(reps, 1))
    force       = calculate_force(BARBELL_WEIGHT_KG, velocity, (duration_s / reps) if reps else 0.0)

    result = {
        "trajectory": full_traj,
        "reps": reps,
        "velocity": round(velocity, 2),
        "load_percent": load_pct,
        "1rm": ONE_RM,
        "tut": tut,
        "bar_path": f"{int(path_m)}m {int((path_m % 1) * 100)}cm",
        "bar_path_accuracy_percent": accuracy,
        "fatigue": fatigue,
        "force_per_rep": force,
    }
    return result, output_path