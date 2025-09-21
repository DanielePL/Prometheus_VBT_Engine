import asyncio, json, uuid, shutil, os, time, threading, traceback
from typing import Dict, Set
import cv2, numpy as np
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from starlette.concurrency import run_in_threadpool
from ultralytics import YOLO

app = FastAPI()

BAR_CLASS_NAME      = "barbell"
JPEG_QUALITY_LIVE   = 60
PROGRESS_EVERY_N    = 10

INF_MAX_SIDE        = 960
EMA_ALPHA           = 0.25
MAX_JUMP_FRAC       = 0.035

YOLO_MODEL_PATH = "app/models/best.pt"
_model = None
_model_lock = threading.Lock()
BAR_CLASS_IDS = None

def get_model():
    """Лениво грузим модель и определяем ID нужного класса."""
    global _model, BAR_CLASS_IDS
    if _model is None:
        with _model_lock:
            if _model is None:
                print("[YOLO] Loading model:", YOLO_MODEL_PATH, flush=True)
                m = YOLO(YOLO_MODEL_PATH)
                names = m.model.names
                ids = [cid for cid, name in names.items() if name == BAR_CLASS_NAME] or list(names.keys())
                _model = m
                BAR_CLASS_IDS = ids
                print(f"[YOLO] Loaded. BAR_CLASS_IDS={BAR_CLASS_IDS}", flush=True)
    return _model


class WSManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}

    async def connect(self, room: str, ws: WebSocket):
        await ws.accept()
        self.rooms.setdefault(room, set()).add(ws)

    def disconnect(self, room: str, ws: WebSocket):
        if room in self.rooms and ws in self.rooms[room]:
            self.rooms[room].remove(ws)
            if not self.rooms[room]:
                self.rooms.pop(room, None)

    async def broadcast_json(self, room: str, data: dict):
        dead = []
        for ws in self.rooms.get(room, []):
            try:
                await ws.send_text(json.dumps(data, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(room, ws)

manager = WSManager()

@app.get("/")
async def root():
    return {"ok": True}

@app.post("/api/v1/process_video_ws")
async def process_video_ws(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    temp_input  = f"/tmp/input_{job_id}.mp4"
    temp_output = f"/tmp/output_{job_id}.mp4"

    with open(temp_input, "wb") as f:
        shutil.copyfileobj(file.file, f)

    asyncio.create_task(run_stream_job(job_id, temp_input, temp_output))
    return JSONResponse({"job_id": job_id})

@app.websocket("/ws/process/{job_id}")
async def ws_process(websocket: WebSocket, job_id: str):
    await manager.connect(job_id, websocket)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "keepalive"}))
    except WebSocketDisconnect:
        manager.disconnect(job_id, websocket)

@app.get("/api/v1/download/{name}")
async def download(name: str):
    path = f"/tmp/{name}"
    return FileResponse(path, media_type="video/mp4", filename="processed.mp4")

async def run_stream_job(job_id: str, temp_input: str, temp_output: str):
    try:
        await manager.broadcast_json(job_id, {"type": "status", "state": "started", "job_id": job_id})

        cap = cv2.VideoCapture(temp_input)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open input video: {temp_input}")

        fps  = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1)
        fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480

        await manager.broadcast_json(job_id, {"type": "frame_info", "w": fw, "h": fh})

        diag = (fw**2 + fh**2) ** 0.5
        max_jump_px = MAX_JUMP_FRAC * diag

        model = get_model()

        frame_idx = 0
        last_center = None
        traj_by_frame: dict[int, tuple[int, int]] = {}

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            scale = 1.0
            max_side = max(fw, fh)
            if max_side > INF_MAX_SIDE:
                scale = INF_MAX_SIDE / max_side
                inf_w, inf_h = int(fw * scale), int(fh * scale)
                inf_img = cv2.resize(frame, (inf_w, inf_h), interpolation=cv2.INTER_AREA)
            else:
                inf_img = frame

            res = await run_in_threadpool(
                model.predict,
                inf_img,
                stream=False,
                classes=BAR_CLASS_IDS,
                conf=0.45,
                iou=0.50,
                imgsz=640,
                max_det=25,
                verbose=False
            )
            boxes = res[0].boxes if res else None

            chosen = None
            if boxes is not None and len(boxes) > 0:
                confs = boxes.conf.cpu().numpy()
                xyxy = boxes.xyxy.cpu().numpy()
                cand = []
                for i in range(len(xyxy)):
                    x1, y1, x2, y2 = xyxy[i]
                    cx = (x1 + x2) * 0.5
                    cy = (y1 + y2) * 0.5
                    cand.append((cx, cy, float(confs[i])))

                if cand:
                    if last_center is None:
                        cx, cy, _ = max(cand, key=lambda t: t[2])
                    else:
                        lcx_inf = last_center[0] * scale
                        lcy_inf = last_center[1] * scale
                        cx, cy, _ = min(cand, key=lambda t: (t[0] - lcx_inf) ** 2 + (t[1] - lcy_inf) ** 2)

                    cx_o = float(cx) / (scale or 1.0)
                    cy_o = float(cy) / (scale or 1.0)

                    if last_center is not None:
                        jump = ((cx_o - last_center[0]) ** 2 + (cy_o - last_center[1]) ** 2) ** 0.5
                        if jump > max_jump_px:
                            chosen = last_center
                        else:
                            sx = int(EMA_ALPHA * cx_o + (1 - EMA_ALPHA) * last_center[0])
                            sy = int(EMA_ALPHA * cy_o + (1 - EMA_ALPHA) * last_center[1])
                            chosen = (sx, sy)
                    else:
                        chosen = (int(cx_o), int(cy_o))
            else:
                chosen = last_center

            if chosen is not None:
                x = int(np.clip(chosen[0], 0, fw - 1))
                y = int(np.clip(chosen[1], 0, fh - 1))
                last_center = (x, y)
                traj_by_frame[frame_idx] = (x, y)

            if frame_idx % PROGRESS_EVERY_N == 0:
                await manager.broadcast_json(job_id, {
                    "type": "progress",
                    "phase": "detect",
                    "frame": frame_idx,
                    "total": total,
                    "pct": round(frame_idx * 100 / max(total, 1), 2)
                })

            await asyncio.sleep(0)

        cap.release()

        if len(traj_by_frame) < 2:
            shutil.copyfile(temp_input, temp_output)
            await manager.broadcast_json(job_id, {
                "type": "result",
                "job_id": job_id,
                "metrics": {"reps": 0, "velocity": 0.0, "tut": 0.0},
                "download": f"/api/v1/download/{os.path.basename(temp_output)}"
            })
            await manager.broadcast_json(job_id, {"type": "status", "state": "finished"})
            return
        min_f = min(traj_by_frame.keys())
        max_f = max(traj_by_frame.keys())
        frozen_traj = [traj_by_frame[f] for f in range(min_f, max_f + 1) if f in traj_by_frame]

        cap2 = cv2.VideoCapture(temp_input)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(temp_output, fourcc, fps, (fw, fh))
        if not out.isOpened():
            raise RuntimeError("VideoWriter failed to open output file")

        thickness = max(6, (fw + fh) // 100)
        dot_r = max(8, (fw + fh) // 150)

        fidx = 0
        cur_pts: list[tuple[int, int]] = []
        while True:
            ok, frame = cap2.read()
            if not ok:
                break
            fidx += 1

            if fidx >= min_f:
                if fidx <= max_f:
                    if fidx in traj_by_frame:
                        cur_pts.append(traj_by_frame[fidx])
                else:
                    cur_pts = frozen_traj

            if cur_pts:
                pts_np = np.array(cur_pts, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(frame, [pts_np], False, (0, 165, 255), thickness)
                cv2.circle(frame, cur_pts[-1], dot_r, (0, 255, 0), -1)

            out.write(frame)

            if fidx % PROGRESS_EVERY_N == 0:
                await manager.broadcast_json(job_id, {
                    "type": "progress",
                    "phase": "render",
                    "frame": fidx,
                    "total": total,
                    "pct": round(fidx * 100 / max(total, 1), 2)
                })
            await asyncio.sleep(0)

        cap2.release()
        out.release()

        await manager.broadcast_json(job_id, {
            "type": "result",
            "job_id": job_id,
            "metrics": {"frames": total, "fps": fps, "trajectory_points": len(frozen_traj)},
            "download": f"/api/v1/download/{os.path.basename(temp_output)}"
        })
        await manager.broadcast_json(job_id, {"type": "status", "state": "finished"})

    except Exception as e:
        print("run_stream_job ERROR:", e, flush=True)
        traceback.print_exc()
        await manager.broadcast_json(job_id, {"type": "status", "state": "error", "error": str(e)})

@app.websocket("/ws/live/{session_id}")
async def ws_live(websocket: WebSocket, session_id: str):
    await websocket.accept()
    try:
        model = get_model()
        trajectory: list[tuple[int, int]] = []
        last_center: tuple[int, int] | None = None
        frame_idx = 0

        sent_frame_info = False
        max_jump_px = None

        while True:
            msg = await websocket.receive()
            if "bytes" not in msg:
                continue

            npimg = np.frombuffer(msg["bytes"], dtype=np.uint8)
            frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            frame_idx += 1
            fh, fw = frame.shape[:2]

            if not sent_frame_info:
                await websocket.send_text(json.dumps({"type": "frame_info", "w": fw, "h": fh}))
                diag = (fw**2 + fh**2) ** 0.5
                max_jump_px = MAX_JUMP_FRAC * diag
                sent_frame_info = True

            scale = 1.0
            max_side = max(fw, fh)
            if max_side > INF_MAX_SIDE:
                scale = INF_MAX_SIDE / max_side
                inf_w, inf_h = int(fw * scale), int(fh * scale)
                inf_img = cv2.resize(frame, (inf_w, inf_h), interpolation=cv2.INTER_AREA)
            else:
                inf_img = frame

            res = await run_in_threadpool(
                model.predict,
                inf_img,
                stream=False,
                classes=BAR_CLASS_IDS,
                conf=0.45,
                iou=0.50,
                imgsz=640,
                max_det=25,
                verbose=False,
            )
            boxes = res[0].boxes if res else None

            chosen_center = None
            if boxes is not None and len(boxes) > 0:
                confs = boxes.conf.cpu().numpy()
                xyxy = boxes.xyxy.cpu().numpy()
                cand = []
                for i in range(len(xyxy)):
                    x1, y1, x2, y2 = xyxy[i]
                    cx = (x1 + x2) * 0.5
                    cy = (y1 + y2) * 0.5
                    cand.append((cx, cy, float(confs[i])))

                if cand:
                    if last_center is None:
                        cx, cy, _ = max(cand, key=lambda t: t[2])
                    else:
                        lcx_inf = last_center[0] * scale
                        lcy_inf = last_center[1] * scale
                        cx, cy, _ = min(cand, key=lambda t: (t[0] - lcx_inf) ** 2 + (t[1] - lcy_inf) ** 2)

                    cx_o = cx / (scale or 1.0)
                    cy_o = cy / (scale or 1.0)

                    if last_center is not None:
                        jump = ((cx_o - last_center[0]) ** 2 + (cy_o - last_center[1]) ** 2) ** 0.5
                        if jump > (max_jump_px or 1e9):
                            chosen_center = last_center
                        else:
                            sx = int(EMA_ALPHA * cx_o + (1 - EMA_ALPHA) * last_center[0])
                            sy = int(EMA_ALPHA * cy_o + (1 - EMA_ALPHA) * last_center[1])
                            chosen_center = (sx, sy)
                    else:
                        chosen_center = (int(cx_o), int(cy_o))

            if chosen_center is not None:
                last_center = chosen_center
                trajectory.append(last_center)
            elif last_center is not None:
                trajectory.append(last_center)

            if len(trajectory) > 10000:
                trajectory = trajectory[-10000:]

            if trajectory:
                pts_np = np.array(trajectory, dtype=np.int32).reshape((-1, 1, 2))
                thick = max(6, (fw + fh) // 100)
                dot_r = max(8, (fw + fh) // 150)
                cv2.polylines(frame, [pts_np], False, (0, 165, 255), thick)
                cv2.circle(frame, trajectory[-1], dot_r, (0, 255, 0), -1)

            ok, buf = await run_in_threadpool(cv2.imencode, ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY_LIVE])
            if ok:
                await websocket.send_bytes(buf.tobytes())

            await asyncio.sleep(0)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "status", "state": "error", "error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass