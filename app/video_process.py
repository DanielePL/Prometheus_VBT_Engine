import cv2
import os
import numpy as np
from scipy.interpolate import interp1d
from ultralytics import YOLO, solutions
from typing import List, Tuple, Dict
from math import sqrt, hypot

# ===== модель =====
model = YOLO('app/models/yolo11n-pose.pt')

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

def calculate_force(weight: float, velocity: float, time_per_rep: float) -> float:
    acceleration = velocity / time_per_rep if time_per_rep else 0
    return round(weight * acceleration, 3)

def format_gym_results(gym_results, prev_velocity: float = 0.0) -> dict:
    """
    Преобразует результаты AI Gym в необходимый формат
    
    Args:
        gym_results: результаты от solutions.AIGym (объект SolutionResults)
        prev_velocity: предыдущая скорость для определения усталости
        
    Returns:
        Отформатированный словарь с метриками
    """
    try:
        # Проверяем, что gym_results не None
        if gym_results is None:
            return {
                "reps": 0,
                "velocity": 0.0,
                "fatigue": "Without alert",
                "angle": 0.0,
                "stage": "unknown",
                "tracks": 0,
            }
            
        # Преобразуем объект SolutionResults в словарь
        if hasattr(gym_results, '__dict__'):
            results_dict = vars(gym_results)
        else:
            results_dict = dict(gym_results)
        
        # Извлекаем основные метрики
        reps = 0
        if 'workout_count' in results_dict and results_dict['workout_count']:
            reps_list = results_dict['workout_count']
            if isinstance(reps_list, (list, tuple)) and len(reps_list) > 0:
                reps = int(reps_list[0])
            else:
                reps = int(reps_list) if reps_list else 0
        
        # Скорость: используем скорость трека (целевого движения)
        # ВАЖНО: speed['track'] возвращает скорость в пиксель/сек, нужна конвертация!
        velocity = 0.0
        speed_info = results_dict.get('speed', {})
        
        # Коэффициент для конвертации пиксель → метры (из конфига AIGym)
        meter_per_pixel = results_dict.get('meter_per_pixel', 0.005)
        
        if isinstance(speed_info, dict) and 'track' in speed_info:
            # speed['track'] в пиксель/сек → конвертируем в м/сек
            speed_pixel_per_sec = float(speed_info['track'])
            velocity = speed_pixel_per_sec * meter_per_pixel
        
        # Определяем уровень усталости на основе скорости и угла
        workout_angle = 90.0
        if 'workout_angle' in results_dict and results_dict['workout_angle']:
            angle_list = results_dict['workout_angle']
            if isinstance(angle_list, (list, tuple)) and len(angle_list) > 0:
                workout_angle = float(angle_list[0])
            else:
                workout_angle = float(angle_list) if angle_list else 90.0
        
        # Логика определения усталости (с учетом конвертированной скорости):
        # - Если скорость менее 0.1 м/с → "Critical alert" (движение замедлилось сильно)
        # - Если скорость менее 0.5 м/с → "High alert" (движение замедлилось)
        # - Если угол плохой (< 45°) → "Low alert" (неправильная техника)
        # - Иначе → "Without alert"
        
        fatigue = "Without alert"
        
        if velocity < 0.1:
            fatigue = "Critical alert"
        elif velocity < 0.5:
            fatigue = "High alert"
        elif workout_angle < 45.0:
            fatigue = "Low alert"
        
        # Если скорость уменьшается → рост усталости
        if prev_velocity > 0 and velocity < prev_velocity * 0.7:
            if fatigue == "Without alert":
                fatigue = "Low alert"
            elif fatigue == "Low alert":
                fatigue = "High alert"
        
        # Извлекаем этап движения
        stage = 'unknown'
        if 'workout_stage' in results_dict and results_dict['workout_stage']:
            stage_list = results_dict['workout_stage']
            if isinstance(stage_list, (list, tuple)) and len(stage_list) > 0:
                stage = str(stage_list[0])
            else:
                stage = str(stage_list) if stage_list else 'unknown'
        
        # Количество отслеживаемых людей
        tracks = results_dict.get('total_tracks', 0)
        
        return {
            "reps": int(reps),
            "velocity": round(velocity, 2),
            "fatigue": fatigue,
            "angle": round(workout_angle, 1),
            "stage": stage,
            "tracks": int(tracks),
        }
    except Exception as e:
        print(f"❌ Ошибка при форматировании результатов: {e}")
        import traceback
        traceback.print_exc()
        return {
            "reps": 0,
            "velocity": 0.0,
            "fatigue": "Without alert",
            "angle": 0.0,
            "stage": "unknown",
            "tracks": 0,
        }

# ========= основной процесс =========
def process_video(video_path: str, output_path: str) -> Tuple[Dict, str]:

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Входной файл не найден или не может быть открыт: {video_path}")
    frame_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    start_frame = 1                 # не ждём 1 сек — начинаем сразу
    stop_frame  = total_frames      # до конца файла

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (frame_width, frame_height))
    if not out.isOpened():
        cap.release()
        raise FileNotFoundError(f"Не удалось открыть выходной файл для записи: {output_path}")

    frames: List[np.ndarray] = []
    frame_idx = 0

    gym = solutions.AIGym(
        show=True,  # Display the frame
        kpts=[5, 11, 13],  # keypoints index of person for monitoring specific exercise, by default it's for pushup
        model="app/models/yolo11n-pose.pt",  # Path to the YOLO11 pose estimation model file
        line_width=2,  # Adjust the line width for bounding boxes and text display
        verbose=False,
        show_labels=True,
    )
    reps = 0
    velocity = 0.0
    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        frames.append(frame.copy())

        if frame_idx < start_frame or frame_idx > stop_frame:
            continue

        try:
            results = gym(frame)  # monitor workouts on each frame
            formatted_results = format_gym_results(results)
            velocity = formatted_results['velocity']
            if formatted_results['reps'] > 0 and reps < formatted_results['reps']:
                reps += 1
        except Exception as e:
            print(f"Ошибка при обработке кадра {frame_idx}: {e}")
            # Продолжаем обработку с предыдущими значениями
            continue
        

    cap.release()

    # Закрываем VideoWriter если он был создан
    if 'out' in locals() and out.isOpened():
        out.release()
    
    result = {
        "trajectory": [],
        "reps": reps,
        "velocity": round(velocity, 2),
        "load_percent": '0%',
        "1rm": ONE_RM,
        "tut": 0.0,
        "bar_path": "0m 0cm",
        "bar_path_accuracy_percent": 100.0,
        "fatigue": "Without alert",
        "force_per_rep": 0.0,
    }
    return result, output_path