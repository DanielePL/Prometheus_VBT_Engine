import cv2
import numpy as np
from scipy.interpolate import interp1d
from ultralytics import YOLO
from typing import List, Tuple, Dict
from math import sqrt


model = YOLO('app/models/best.pt')

BARBELL_WEIGHT_KG = 60
ONE_RM = 90
PX_TO_METER = 1000
IDEAL_PATH_TYPE = 'line'

def calculate_reps(y_positions: List[int]) -> int:
    reps = 0
    for i in range(1, len(y_positions) - 1):
        if y_positions[i - 1] > y_positions[i] < y_positions[i + 1]:
            reps += 1
    return max(reps, 1)


def calculate_bar_path_length(points: List[Tuple[int, int]]) -> float:
    length_px = sum(
        sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
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
    x_start, y_start = points[0]
    x_end, y_end = points[-1]
    if y_end == y_start:
        return 100.0

    ideal_line = interp1d([y_start, y_end], [x_start, x_end])
    deviation = 0
    for x, y in points:
        ideal_x = float(ideal_line(y))
        deviation += abs(ideal_x - x)
    avg_dev = deviation / len(points)
    return round(100 - (avg_dev / frame_width * 100), 2)


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

def process_video(video_path: str, output_path: str) -> Tuple[Dict, str]:
    cap = cv2.VideoCapture(video_path)
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    start_frame = int(fps * 1)
    stop_frame = total_frames

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (frame_width, frame_height))

    desired_classes = ['person', 'barbell']
    trajectory_points = {}
    frames = []
    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        frames.append(frame.copy())

        if frame_idx < start_frame:
            continue
        if frame_idx <= stop_frame:
            results = model(frame)
            boxes = results[0].boxes
            classes = results[0].names
            for box in boxes:
                class_id = int(box.cls)
                class_name = classes[class_id]
                if class_name not in desired_classes:
                    continue
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                center = ((x1 + x2) // 2, (y1 + y2) // 2)
                trajectory_points[frame_idx] = center

    cap.release()

    # Интерполяция
    frames_found = np.array(sorted(trajectory_points.keys()))
    points = np.array([trajectory_points[f] for f in frames_found])
    interp_x = interp1d(frames_found, points[:, 0], kind='linear')
    interp_y = interp1d(frames_found, points[:, 1], kind='linear')

    interpolated = {
        f: (int(interp_x(f)), int(interp_y(f)))
        for f in range(start_frame, stop_frame + 1)
        if f in range(frames_found[0], frames_found[-1] + 1)
    }

    final_trajectory = [interpolated[f] for f in range(start_frame, stop_frame + 1) if f in interpolated]

    # Отрисовка
    avg_size = (frame_width + frame_height) / 2
    radius = max(int(avg_size * 0.01), 4)
    for i, frame in enumerate(frames):
        idx = i + 1
        if idx < start_frame:
            out.write(frame)
            continue

        pts = [interpolated[f] for f in range(start_frame, idx + 1) if f in interpolated]
        if not pts:
            out.write(frame)
            continue

        cv2.polylines(frame, [np.array(pts, dtype=np.int32).reshape((-1, 1, 2))],
                      isClosed=False, color=(0, 165, 255), thickness=radius)
        out.write(frame)

    out.release()

    y_positions = [p[1] for p in final_trajectory]
    reps = calculate_reps(y_positions)
    path_m = calculate_bar_path_length(final_trajectory)
    duration_s = len(final_trajectory) / fps
    velocity = calculate_avg_velocity(path_m, duration_s)
    load_percent = calculate_load_percent(BARBELL_WEIGHT_KG, ONE_RM)
    tut = calculate_tut(len(final_trajectory), fps)
    accuracy = calculate_accuracy(final_trajectory, frame_width)
    fatigue = detect_fatigue([velocity]*reps, [accuracy]*reps)  # эмуляция по среднему
    force = calculate_force(BARBELL_WEIGHT_KG, velocity, duration_s / reps)

    result = {
        "trajectory": final_trajectory,
        "reps": reps,
        "velocity": round(velocity, 2),
        "load_percent": load_percent,
        "1rm": ONE_RM,
        "tut": tut,
        "bar_path": f"{int(path_m)}m {int((path_m % 1) * 100)}cm",
        "bar_path_accuracy_percent": accuracy,
        "fatigue": fatigue,
        "force_per_rep": force
    }

    return result, output_path