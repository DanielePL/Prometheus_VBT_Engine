import asyncio
import json
import os
import uuid
import traceback
from typing import Dict, Optional

import cv2
import numpy as np
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.rtcrtpparameters import RTCRtpCodecCapability
from aiortc.rtcconfiguration import RTCConfiguration
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
from ultralytics import YOLO

app = FastAPI()

BAR_CLASS_NAME = "barbell"
YOLO_MODEL_PATH = "app/models/best.pt"
EMA_ALPHA = 0.25
MAX_JUMP_FRAC = 0.035

_model: Optional[YOLO] = None
BAR_CLASS_IDS: Optional[list[int]] = None

def get_model() -> YOLO:
    global _model, BAR_CLASS_IDS
    if _model is None:
        m = YOLO(YOLO_MODEL_PATH)
        names = m.model.names
        ids = [cid for cid, name in names.items() if name == BAR_CLASS_NAME] or list(names.keys())
        _model = m
        BAR_CLASS_IDS = ids
        print(f"[YOLO] loaded; BAR_CLASS_IDS={ids}", flush=True)
    return _model

class ProcessedTrack(MediaStreamTrack):
    kind = "video"
    def __init__(self):
        super().__init__()
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=2)
        self.ended = False
    async def push(self, bgr: np.ndarray):
        if self.ended:
            return
        while self.queue.qsize() >= 2:
            try:
                _ = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self.queue.put(bgr)
    async def recv(self) -> VideoFrame:
        frame_bgr = await self.queue.get()
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        vf = VideoFrame.from_ndarray(rgb, format="rgb24")
        vf.pts, vf.time_base = None, None
        return vf

class Session:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.tmp = f"/tmp/{job_id}.mp4"
        self.file = open(self.tmp, "wb")
        self.expected: Optional[int] = None
        self.received = 0
        self.control = None
        self.processed = ProcessedTrack()
        self.upload_done = asyncio.Event()
        self.closed = False
        self.last_center: Optional[tuple[int, int]] = None
        self.max_jump_px: Optional[float] = None

    def close_file(self):
        try:
            if not self.file.closed:
                self.file.close()
        except:
            pass

sessions: Dict[str, Session] = {}
pcs: Dict[str, RTCPeerConnection] = {}

async def send_control(sess: Session, data: dict):
    ch = sess.control
    if ch and ch.readyState == "open":
        try:
            ch.send(json.dumps(data, ensure_ascii=False))
        except:
            pass

async def process_and_stream(sess: Session):
    print("run job")
    try:
        cap = cv2.VideoCapture(sess.tmp)
        if not cap.isOpened():
            await send_control(sess, {"type": "status", "state": "error", "error": "open failed"})
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
        diag = (fw**2 + fh**2) ** 0.5
        sess.max_jump_px = MAX_JUMP_FRAC * diag
        model = get_model()
        last = None
        thickness = max(6, (fw + fh) // 100)
        dot_r = max(8, (fw + fh) // 150)
        traj: list[tuple[int,int]] = []

        await send_control(sess, {"type": "status", "state": "processing"})
        interval = 1.0 / float(fps if fps > 1e-3 else 25.0)

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            res = model.predict(
                frame,
                stream=False,
                classes=BAR_CLASS_IDS,
                conf=0.45,
                iou=0.5,
                imgsz=640,
                max_det=25,
                verbose=False,
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
                if last is None:
                    cx, cy, _ = max(cand, key=lambda t: t[2])
                    chosen = (int(cx), int(cy))
                else:
                    lcx, lcy = last
                    cx, cy, _ = min(cand, key=lambda t: (t[0]-lcx)**2 + (t[1]-lcy)**2)
                    jump = ((cx - lcx)**2 + (cy - lcy)**2) ** 0.5
                    if sess.max_jump_px and jump > sess.max_jump_px:
                        chosen = last
                    else:
                        sx = int(EMA_ALPHA * cx + (1-EMA_ALPHA) * lcx)
                        sy = int(EMA_ALPHA * cy + (1-EMA_ALPHA) * lcy)
                        chosen = (sx, sy)
            if chosen is not None:
                last = chosen
                traj.append(chosen)

            if traj:
                pts = np.array(traj, dtype=np.int32).reshape((-1,1,2))
                cv2.polylines(frame, [pts], False, (0,165,255), thickness)
                cv2.circle(frame, traj[-1], dot_r, (0,255,0), -1)

            await sess.processed.push(frame)
            await asyncio.sleep(interval)
            print("trajectory")

        await send_control(sess, {"type": "status", "state": "finished"})
    except Exception as e:
        traceback.print_exc()
        await send_control(sess, {"type": "status", "state": "error", "error": str(e)})

@app.post("/webrtc/process")
async def webrtc_process(offer: dict = Body(...)):
    job_id = str(uuid.uuid4())
    sess = Session(job_id)
    sessions[job_id] = sess

    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    pcs[job_id] = pc

    sender = pc.addTrack(sess.processed)

    @pc.on("datachannel")
    def on_datachannel(ch):
        if ch.label == "control":
            sess.control = ch
        elif ch.label == "upload":
            @ch.on("message")
            async def on_upload(msg):
                if isinstance(msg, bytes):
                    if not sess.file.closed:
                        sess.file.write(msg)
                        sess.received += len(msg)
                        if sess.expected:
                            pct = round(sess.received * 100.0 / sess.expected, 2)
                            await send_control(sess, {"type": "progress", "phase": "upload", "pct": pct})
                else:
                    try:
                        data = json.loads(msg)
                    except:
                        return
                    t = data.get("type")
                    if t == "start":
                        sess.expected = int(data.get("size") or 0) or None
                        await send_control(sess, {"type": "status", "state": "uploading", "job_id": job_id})
                    elif t == "end":
                        sess.close_file()
                        print(1)
                        await send_control(sess, {"type": "status", "state": "processing"})
                        print(2)
                        await process_and_stream(sess)

    offer_sdp = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
    await pc.setRemoteDescription(offer_sdp)

    codecs = [c for c in sender.getCapabilities("video").codecs if isinstance(c, RTCRtpCodecCapability) and c.name.upper() == "VP8"]
    if codecs:
        for tr in pc.getTransceivers():
            if tr.sender == sender:
                tr.setCodecPreferences(codecs)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    async def runner():
       pass

    return JSONResponse({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "job_id": job_id})