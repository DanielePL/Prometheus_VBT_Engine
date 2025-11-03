import cv2
import os
import numpy as np
from ultralytics import YOLO
from typing import Tuple, Dict

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
def process_video(
    video_path: str, 
    output_path: str,
    meters_per_pixel: float = 0.002,
    weight_kg: float = 100,
    one_rm_kg: float = 90,
) -> Tuple[Dict, str]:

    model_detect = YOLO(str(model))

    # Open video for reading and writing
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"[ERR] Failed to open video: {video_path}")
        return None, None
    
    # Get video params
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    # Read first frame before initializing writer to take actual frame size
    ret_first, first_frame = cap.read()
    if not ret_first:
        print(f"[ERR] Empty video: {video_path}")
        cap.release()
        return None, None

    # print(first_frame.shape, cap.get(cv2.CAP_PROP_ORIENTATION_META))
    # if first_frame.shape[0] < first_frame.shape[1]:
    #     first_frame = cv2.flip(first_frame, 1)
    # first_frame = cv2.rotate(first_frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

    frame_height, frame_width = first_frame.shape[0], first_frame.shape[1]
    
    # Extra space for the stats panel (below the frame) - увеличен для всех метрик
    panel_extra_height = max(220, int(frame_height * 0.35))

    # Create output file with extended height (frame + panel)
    # output_path = output_dir / "detect" / video_path.name
    # output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (frame_width, frame_height + panel_extra_height))
    
    # Lines and trajectory variables
    first_frame_center_x = None  # x of first detection center (blue line)
    trajectory = []  # All raw center points for path drawing
    
    # Smoothing variables
    smoothed_trajectory = []  # Smoothed points
    alpha = 0.3  # Smoothing factor (0.0-1.0, lower = stronger smoothing)
    last_x = None
    last_y = None
    
    # Reps and speed variables
    reps_count = 0
    half_reps = 0
    last_direction = 0  # 1 = down, -1 = up, 0 = undefined
    last_extremum_y = None
    min_movement_pixels = max(10, int(frame_height * 0.01))
    prev_smoothed_y = None
    current_speed_px_s = 0.0
    max_speed_px_s = 0.0
    speed_sum_abs = 0.0
    speed_samples = 0
    # Velocity in m/s if meters_per_pixel provided
    current_velocity_m_s = 0.0
    max_velocity_m_s = 0.0
    velocity_sum_abs = 0.0
    current_velocity_signed_m_s = 0.0
    
    # Additional VBT metrics variables
    # 1RM and Load
    calculated_one_rm = None  # Will be calculated if reps_count > 0
    
    # Ideal trajectory for path accuracy (use first rep as reference)
    ideal_trajectory: list[tuple[int, int]] = []
    path_deviations: list[float] = []
    path_accuracy_percent = 100.0
    total_path_distance = 0.0
    
    # Per-rep tracking
    rep_data: list[dict] = []
    current_rep_start_frame = None
    current_rep_start_time = None
    current_rep_velocities: list[float] = []
    current_rep_accuracies: list[float] = []
    
    # Peak velocities (separate for up/down)
    peak_velocity_up = 0.0
    peak_velocity_down = 0.0
    current_rep_peak_up = 0.0
    current_rep_peak_down = 0.0
    
    # Time Under Tension
    total_tut = 0.0  # Total time under tension
    current_rep_duration = 0.0
    
    # Force calculation
    prev_velocity = 0.0
    current_acceleration = 0.0
    max_force = 0.0
    current_force = 0.0
    
    # Fatigue detection
    fatigue_warnings: list[str] = []
    velocities_per_rep: list[float] = []
    accuracy_per_rep: list[float] = []
    
    frame_idx = 0
    # Process the first frame we already read
    frame = first_frame
    ret = True
    while True:
        
        frame_idx += 1
        print(frame.shape, frame.dtype)
        # Detection with tracking
        results_detect = model_detect.track(
            frame,
            tracker="bytetrack.yaml",
            persist=True,
            conf=0.25,
            device="cpu",
            verbose=False,
        )
        
        # Draw detections and lines
        if results_detect and len(results_detect) > 0:
            result = results_detect[0]
            
            # Parse detection boxes
            if result.boxes is not None and len(result.boxes) > 0:
                # Берём первую рамку (ID=1 или самая уверенная)
                boxes = result.boxes
                if len(boxes) > 0:
                    # Take first box
                    box = boxes[0]
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    center_x = int((x1 + x2) / 2)
                    center_y = int((y1 + y2) / 2)
                    
                    # Save first center for fixed line
                    if first_frame_center_x is None:
                        first_frame_center_x = center_x
                    
                    # Smooth center coordinates
                    if last_x is None or last_y is None:
                        smoothed_x = center_x
                        smoothed_y = center_y
                        last_x = center_x
                        last_y = center_y
                    else:
                        # EMA smoothing
                        smoothed_x = int(alpha * center_x + (1 - alpha) * last_x)
                        smoothed_y = int(alpha * center_y + (1 - alpha) * last_y)
                        last_x = smoothed_x
                        last_y = smoothed_y
                    
                    # Append trajectory points
                    trajectory.append((center_x, center_y))
                    smoothed_trajectory.append((smoothed_x, smoothed_y))
                    
                    # Limit trajectory length
                    max_trajectory_length = 5000
                    if len(trajectory) > max_trajectory_length:
                        trajectory.pop(0)
                    if len(smoothed_trajectory) > max_trajectory_length:
                        smoothed_trajectory.pop(0)
                    
                    # Draw box
                    cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
                    
                    # Draw track id if present
                    if box.id is not None:
                        track_id = int(box.id[0])
                        cv2.putText(frame, f'ID: {track_id}', (int(x1), int(y1) - 10),
                                  cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                    
                    # Draw vertical lines
                    # Blue: fixed (first frame center)
                    if first_frame_center_x is not None:
                        cv2.line(frame, (first_frame_center_x, 0), (first_frame_center_x, frame_height), 
                                (255, 0, 0), 3)  # Синяя линия
                    
                    # Red: current center
                    cv2.line(frame, (center_x, 0), (center_x, frame_height), 
                            (0, 0, 255), 2)  # Красная линия (движущаяся)
                    
                    # Draw trajectories
                    # Raw trajectory (thin pale)
                    if len(trajectory) > 1:
                        for i in range(1, len(trajectory)):
                            pt1 = trajectory[i-1]
                            pt2 = trajectory[i]
                            # Thin pale yellow line
                            cv2.line(frame, pt1, pt2, (0, 100, 150), 1)
                    
                    # Smoothed trajectory
                    if len(smoothed_trajectory) > 1:
                        # Gradient: older -> paler, newer -> brighter
                        for i in range(1, len(smoothed_trajectory)):
                            pt1 = smoothed_trajectory[i-1]
                            pt2 = smoothed_trajectory[i]
                            
                            # Color intensity
                            intensity = int(255 * (i / len(smoothed_trajectory)))
                            
                            # Bright yellow gradient (BGR)
                            color = (0, intensity, 255)
                            
                            cv2.line(frame, pt1, pt2, color, 2)
                        
                        # Current smoothed center point
                        cv2.circle(frame, (smoothed_x, smoothed_y), 5, (0, 255, 255), -1)

                        # Compute vertical speed (px/s)
                        current_time = frame_idx / fps if fps > 0 else 0.0
                        if prev_smoothed_y is not None and fps > 0:
                            dy = smoothed_y - prev_smoothed_y
                            current_speed_px_s = abs(dy) * fps
                            max_speed_px_s = max(max_speed_px_s, current_speed_px_s)
                            speed_sum_abs += current_speed_px_s
                            speed_samples += 1
                            if meters_per_pixel is not None:
                                current_velocity_m_s = current_speed_px_s * meters_per_pixel
                                max_velocity_m_s = max(max_velocity_m_s, current_velocity_m_s)
                                velocity_sum_abs += current_velocity_m_s
                                current_velocity_signed_m_s = (-dy * fps) * meters_per_pixel
                            else:
                                current_velocity_signed_m_s = -dy * fps
                            
                            # Peak velocity tracking (up/down separate)
                            if current_velocity_signed_m_s > 0:  # Moving up
                                current_rep_peak_up = max(current_rep_peak_up, abs(current_velocity_signed_m_s))
                                peak_velocity_up = max(peak_velocity_up, abs(current_velocity_signed_m_s))
                            else:  # Moving down
                                current_rep_peak_down = max(current_rep_peak_down, abs(current_velocity_signed_m_s))
                                peak_velocity_down = max(peak_velocity_down, abs(current_velocity_signed_m_s))
                            
                            # Acceleration and Force calculation (F = W × a, a = ΔV/Δt)
                            if prev_velocity != 0.0 and fps > 0:
                                delta_v = current_velocity_signed_m_s - prev_velocity
                                delta_t_frame = 1.0 / fps
                                if meters_per_pixel is not None:
                                    current_acceleration = delta_v / delta_t_frame if delta_t_frame > 0 else 0.0
                                    if weight_kg is not None:
                                        current_force = weight_kg * abs(current_acceleration)
                                        max_force = max(max_force, current_force)
                            prev_velocity = current_velocity_signed_m_s
                            
                            # Path accuracy calculation (d = √((x2-x1)²+(y2-y1)²))
                            if first_frame_center_x is not None:
                                # Use first_frame_center_x as ideal X, use current smoothed as actual
                                ideal_x = first_frame_center_x
                                ideal_y = smoothed_y  # For now, use current Y (can be improved with ideal trajectory)
                                actual_x = smoothed_x
                                actual_y = smoothed_y
                                deviation = np.sqrt((actual_x - ideal_x)**2 + (actual_y - ideal_y)**2)
                                if meters_per_pixel is not None:
                                    deviation_m = deviation * meters_per_pixel
                                else:
                                    deviation_m = deviation
                                path_deviations.append(deviation_m)
                                if len(path_deviations) > 1000:  # Limit size
                                    path_deviations.pop(0)
                                
                                # Calculate total path distance
                                if len(smoothed_trajectory) > 1:
                                    prev_pt = smoothed_trajectory[-2]
                                    curr_pt = (smoothed_x, smoothed_y)
                                    segment_dist = np.sqrt((curr_pt[0] - prev_pt[0])**2 + (curr_pt[1] - prev_pt[1])**2)
                                    if meters_per_pixel is not None:
                                        total_path_distance += segment_dist * meters_per_pixel
                                    else:
                                        total_path_distance += segment_dist
                                
                                # Path accuracy %: d% = (Σd)/Δx, A = 100% - d%
                                if total_path_distance > 0:
                                    avg_deviation = sum(path_deviations[-100:]) / min(100, len(path_deviations))
                                    deviation_percent = (avg_deviation / total_path_distance * 100) if total_path_distance > 0 else 0.0
                                    path_accuracy_percent = max(0.0, 100.0 - deviation_percent)

                        # Count reps by direction changes and amplitude
                        rep_completed = False
                        if prev_smoothed_y is not None:
                            delta_y = smoothed_y - prev_smoothed_y
                            new_direction = 1 if delta_y > 0 else (-1 if delta_y < 0 else last_direction)
                            if new_direction != 0 and new_direction != last_direction:
                                extremum_candidate = prev_smoothed_y
                                if last_extremum_y is not None:
                                    amplitude = abs(extremum_candidate - last_extremum_y)
                                    if amplitude >= min_movement_pixels:
                                        half_reps += 1
                                        if half_reps % 2 == 0:
                                            reps_count += 1
                                            rep_completed = True
                                            
                                            # Save completed rep data
                                            if current_rep_start_frame is not None and current_rep_start_time is not None:
                                                rep_duration = current_time - current_rep_start_time
                                                total_tut += rep_duration
                                                avg_rep_velocity = sum(current_rep_velocities) / len(current_rep_velocities) if current_rep_velocities else 0.0
                                                avg_rep_accuracy = sum(current_rep_accuracies) / len(current_rep_accuracies) if current_rep_accuracies else path_accuracy_percent
                                                
                                                velocities_per_rep.append(avg_rep_velocity)
                                                accuracy_per_rep.append(avg_rep_accuracy)
                                                
                                                rep_data.append({
                                                    'rep_num': reps_count,
                                                    'start_frame': current_rep_start_frame,
                                                    'end_frame': frame_idx,
                                                    'duration': rep_duration,
                                                    'peak_velocity_up': current_rep_peak_up,
                                                    'peak_velocity_down': current_rep_peak_down,
                                                    'avg_velocity': avg_rep_velocity,
                                                    'path_accuracy': avg_rep_accuracy,
                                                    'force_max': max(current_force, 0.0),
                                                })
                                                
                                                # Fatigue detection: check if V or A decreased >10% between reps
                                                if len(velocities_per_rep) >= 2:
                                                    v_change = ((velocities_per_rep[-1] - velocities_per_rep[-2]) / velocities_per_rep[-2] * 100) if velocities_per_rep[-2] > 0 else 0.0
                                                    if v_change < -10.0:
                                                        fatigue_warnings.append(f"Velocity decreased {abs(v_change):.1f}% (rep {reps_count-1} to {reps_count})")
                                                
                                                if len(accuracy_per_rep) >= 2:
                                                    a_change = ((accuracy_per_rep[-1] - accuracy_per_rep[-2]) / accuracy_per_rep[-2] * 100) if accuracy_per_rep[-2] > 0 else 0.0
                                                    if a_change < -10.0:
                                                        fatigue_warnings.append(f"Accuracy decreased {abs(a_change):.1f}% (rep {reps_count-1} to {reps_count})")
                                                
                                                # Reset current rep tracking
                                                current_rep_velocities = []
                                                current_rep_accuracies = []
                                                current_rep_peak_up = 0.0
                                                current_rep_peak_down = 0.0
                                
                                # Start new rep tracking
                                if rep_completed:
                                    current_rep_start_frame = frame_idx
                                    current_rep_start_time = current_time
                                elif current_rep_start_frame is None:
                                    current_rep_start_frame = frame_idx
                                    current_rep_start_time = current_time
                                
                                last_extremum_y = extremum_candidate
                                last_direction = new_direction

                        # Track current rep data
                        if current_rep_start_frame is not None:
                            if meters_per_pixel is not None:
                                current_rep_velocities.append(abs(current_velocity_signed_m_s))
                            current_rep_accuracies.append(path_accuracy_percent)
                            current_rep_duration = current_time - current_rep_start_time if current_rep_start_time else 0.0
                        
                        # Update previous smoothed after calculations
                        prev_smoothed_y = smoothed_y

                        # (панель рисуем ниже, для каждого кадра)
        
        # Calculate 1RM if weight and reps available: 1RM = W × (1 + R/30)
        if one_rm_kg is None and weight_kg is not None and reps_count > 0:
            calculated_one_rm = weight_kg * (1 + reps_count / 30.0)
        else:
            calculated_one_rm = one_rm_kg
        
        # Calculate Load %: Load % = (Load/1RM) × 100
        load_percent = None
        if calculated_one_rm is not None and calculated_one_rm > 0 and weight_kg is not None:
            load_percent = (weight_kg / calculated_one_rm) * 100.0
        
        # Сформируем выходной кадр с дополнительным пространством снизу для панели
        out_frame = np.zeros((frame_height + panel_extra_height, frame_width, 3), dtype=frame.dtype)
        out_frame[:frame_height, :, :] = frame

        # Координаты панели на дополнительном пространстве
        fh, fw = frame_height, frame_width
        panel_margin = max(10, int(fh * 0.015))
        panel_left_x = panel_margin
        panel_top_y = fh + panel_margin
        panel_right_x = fw - panel_margin
        panel_bottom_y = fh + panel_extra_height - panel_margin

        # Фон панели
        cv2.rectangle(out_frame, (panel_left_x, panel_top_y), (panel_right_x, panel_bottom_y), (60, 60, 60), -1)
        cv2.rectangle(out_frame, (panel_left_x, panel_top_y), (panel_right_x, panel_bottom_y), (100, 100, 100), 2)

        # Заголовок
        title = "PROMETEUS MODEL - VBT Analysis"
        cv2.putText(out_frame, title, (panel_left_x + 10, panel_top_y + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 215, 255), 2)

        # Разделитель
        sep_y = panel_top_y + 40
        cv2.line(out_frame, (panel_left_x + 5, sep_y), (panel_right_x - 5, sep_y), (120, 120, 120), 2)

        # Разделение панели на колонки
        panel_width = panel_right_x - panel_left_x
        col1_x = panel_left_x + 15
        col2_x = panel_left_x + int(panel_width * 0.33)
        col3_x = panel_left_x + int(panel_width * 0.66)
        col4_x = panel_right_x - 15
        
        # Вертикальные линии разделения колонок
        cv2.line(out_frame, (col2_x, sep_y + 5), (col2_x, panel_bottom_y - 5), (80, 80, 80), 1)
        cv2.line(out_frame, (col3_x, sep_y + 5), (col3_x, panel_bottom_y - 5), (80, 80, 80), 1)

        # Размеры шрифтов
        font_scale_main = 0.7
        font_scale_sub = 0.6
        font_scale_small = 0.55
        line_height = 28
        start_y = sep_y + 25

        state_text = "CONCENTRIC" if current_velocity_signed_m_s >= 0 else "ECCENTRIC"
        state_color = (0, 200, 0) if state_text == "CONCENTRIC" else (0, 140, 255)
        vel_unit = "m/s" if meters_per_pixel is not None else "px/s"
        vel_value = current_velocity_signed_m_s

        # КОЛОНКА 1 (ЛЕВАЯ): Основные показатели
        y = start_y
        cv2.putText(out_frame, f"Reps: {reps_count}", (col1_x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_main, (230, 230, 230), 2)
        y += line_height
        cv2.putText(out_frame, f"State: {state_text}", (col1_x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_main, state_color, 2)
        y += line_height
        if meters_per_pixel is not None:
            cv2.putText(out_frame, f"Peak Up: {peak_velocity_up:.2f} m/s", (col1_x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (0, 255, 100), 2)
            y += line_height
            cv2.putText(out_frame, f"Peak Down: {peak_velocity_down:.2f} m/s", (col1_x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (0, 150, 255), 2)
            y += line_height
        cv2.putText(out_frame, f"Path Accuracy: {path_accuracy_percent:.1f}%", (col1_x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (100, 255, 255), 2)

        # КОЛОНКА 2 (ЦЕНТР-ЛЕВО): Скорость и сила
        y = start_y
        cv2.putText(out_frame, f"Velocity: {vel_value:.2f} {vel_unit}", (col2_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_main, (230, 230, 230), 2)
        y += line_height
        if meters_per_pixel is not None and speed_samples > 0:
            avg_vel = velocity_sum_abs / speed_samples
            cv2.putText(out_frame, f"Avg Velocity: {avg_vel:.2f} m/s", (col2_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (200, 200, 200), 2)
            y += line_height
        if max_force > 0:
            cv2.putText(out_frame, f"Max Force: {max_force:.1f} N", (col2_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (255, 150, 100), 2)
            y += line_height
        
        # Предупреждения об усталости
        if fatigue_warnings:
            latest_warning = fatigue_warnings[-1] if len(fatigue_warnings) > 0 else ""
            if len(latest_warning) > 0:
                # Обрезаем слишком длинное сообщение под ширину колонки
                max_warning_len = 45
                warning_text = latest_warning[:max_warning_len] if len(latest_warning) > max_warning_len else latest_warning
                cv2.putText(out_frame, f"! {warning_text}", (col2_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_small, (0, 100, 255), 2)

        # КОЛОНКА 3 (ЦЕНТР-ПРАВО): Вес и нагрузка
        y = start_y
        if weight_kg is not None:
            weight_text = f"Weight: {weight_kg:.1f} kg"
            cv2.putText(out_frame, weight_text, (col3_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_main, (230, 230, 230), 2)
            y += line_height
            if load_percent is not None:
                cv2.putText(out_frame, f"Load: {load_percent:.1f}%", (col3_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (255, 200, 100), 2)
                y += line_height
        if calculated_one_rm is not None:
            cv2.putText(out_frame, f"1RM: {calculated_one_rm:.1f} kg", (col3_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (255, 200, 0), 2)
            y += line_height
        if current_rep_duration > 0:
            cv2.putText(out_frame, f"TUT: {total_tut:.1f}s", (col3_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_sub, (200, 200, 255), 2)
            y += line_height
            cv2.putText(out_frame, f"Current: {current_rep_duration:.1f}s", (col3_x + 10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale_small, (180, 180, 255), 2)

        # Technique Score (based on accuracy and consistency)
        tech_score = int(path_accuracy_percent)
        if len(velocities_per_rep) >= 2:
            # Penalize for high velocity variance
            vel_std = np.std(velocities_per_rep) if len(velocities_per_rep) > 1 else 0.0
            vel_mean = np.mean(velocities_per_rep) if len(velocities_per_rep) > 0 else 1.0
            consistency_penalty = min(20, int((vel_std / vel_mean * 100) if vel_mean > 0 else 20))
            tech_score = max(0, tech_score - consistency_penalty)
        
        score_color = (0, 255, 0) if tech_score >= 80 else ((0, 255, 255) if tech_score >= 60 else (0, 100, 255))
        cv2.putText(out_frame, f"Technique Score: {tech_score}/100", (panel_left_x + 10, panel_bottom_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.75, score_color, 2)

        # Записываем кадр с панелью
        out.write(out_frame)
        
        if frame_idx % 100 == 0:
            print(f"Processed frames: {frame_idx}/{total_frames}")
        
        # Читаем следующий кадр
        ret, frame = cap.read()
        if not ret:
            break
    
    cap.release()
    out.release()
    print(f"[OK] Done. Results at: {output_path}")
    if speed_samples > 0:
        print(f"[STATS] Reps: {reps_count}")
        if meters_per_pixel is not None:
            avg_velocity = velocity_sum_abs / speed_samples if speed_samples else 0.0
            print(f"[STATS] Avg velocity: {avg_velocity:.2f} m/s, Max: {max_velocity_m_s:.2f} m/s")
        else:
            avg_speed = speed_sum_abs / speed_samples
            print(f"[STATS] Avg velocity: {avg_speed:.1f} px/s, Max: {max_speed_px_s:.1f} px/s")
    else:
        print(f"[STATS] Reps: {reps_count}")

    
    result = {
        "trajectory": trajectory,
        "reps": reps_count,
        "velocity": round(max_velocity_m_s, 2),
        "load_percent": load_percent,
        "1rm": calculated_one_rm,
        "tut": total_tut,
        "bar_path": total_path_distance,
        "bar_path_accuracy_percent": path_accuracy_percent,
        "fatigue": fatigue_warnings,
        "force_per_rep": max_force,
    }
    return result, output_path