import asyncio
import os
import json
import logging
import uuid
from typing import Dict, Optional
from datetime import datetime
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.signaling import TcpSocketSignaling
import av
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import threading
import queue

from app.video_process import process_video

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="NeiroFitnessApp WebRTC", version="1.0.0")

# Конфигурация
class Config:
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
    TEMP_DIR = "/tmp/neirofitness"
    OUTPUT_DIR = "/tmp/neirofitness/output"
    FRAME_RATE = 30
    FRAME_WIDTH = 640
    FRAME_HEIGHT = 480

# Создаем директории если не существуют
os.makedirs(Config.TEMP_DIR, exist_ok=True)
os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

# Модели данных
class StreamSession(BaseModel):
    session_id: str
    client_id: str
    status: str = "active"  # active, processing, completed, failed
    created_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[Dict] = None
    output_file: Optional[str] = None
    error_message: Optional[str] = None

class ProcessingResult(BaseModel):
    session_id: str
    status: str
    result: Optional[Dict] = None
    error_message: Optional[str] = None

# Глобальное хранилище сессий
stream_sessions: Dict[str, StreamSession] = {}
active_connections: Dict[str, WebSocket] = {}

# Кастомный VideoStreamTrack для обработки кадров
class ProcessedVideoTrack(VideoStreamTrack):
    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id
        self.frame_queue = queue.Queue(maxsize=30)
        self.processing = False
        self.result = None
        
    async def recv(self):
        """Получение обработанного кадра"""
        try:
            # Получаем кадр из очереди
            frame = self.frame_queue.get(timeout=1.0)
            
            # Создаем AV Frame
            av_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
            av_frame.pts = None
            av_frame.time_base = None
            
            return av_frame
        except queue.Empty:
            # Возвращаем пустой кадр если очередь пуста
            empty_frame = np.zeros((Config.FRAME_HEIGHT, Config.FRAME_WIDTH, 3), dtype=np.uint8)
            av_frame = av.VideoFrame.from_ndarray(empty_frame, format="bgr24")
            av_frame.pts = None
            av_frame.time_base = None
            return av_frame
        except Exception as e:
            logger.error(f"Ошибка в recv: {e}")
            raise

    def add_frame(self, frame: np.ndarray):
        """Добавление кадра в очередь"""
        try:
            if not self.frame_queue.full():
                self.frame_queue.put(frame)
        except Exception as e:
            logger.error(f"Ошибка добавления кадра: {e}")

    def set_result(self, result: Dict):
        """Установка результата обработки"""
        self.result = result
        self.processing = False

# Функция обработки видео в отдельном потоке
def process_video_stream(session_id: str, video_path: str):
    """Обработка видео потока в отдельном потоке"""
    try:
        logger.info(f"Начинаем обработку видео для сессии {session_id}")
        
        # Обновляем статус сессии
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "processing"
        
        # Обрабатываем видео используя существующую логику
        output_path = os.path.join(Config.TEMP_DIR, f"output_{session_id}.mp4")
        result, processed_path = process_video(video_path, output_path)
        
        # Перемещаем результат в выходную директорию
        final_output = os.path.join(Config.OUTPUT_DIR, f"{session_id}_processed.mp4")
        os.rename(processed_path, final_output)
        
        # Обновляем статус сессии
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "completed"
            stream_sessions[session_id].completed_at = datetime.now()
            stream_sessions[session_id].result = result
            stream_sessions[session_id].output_file = final_output
        
        logger.info(f"Обработка завершена для сессии {session_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке видео для сессии {session_id}: {e}")
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "failed"
            stream_sessions[session_id].error_message = str(e)
            stream_sessions[session_id].completed_at = datetime.now()

# WebRTC обработчик
class WebRTCHandler:
    def __init__(self):
        self.pcs = set()
        self.video_tracks = {}
    
    async def handle_offer(self, offer: str, session_id: str) -> str:
        """Обработка WebRTC offer"""
        pc = RTCPeerConnection()
        self.pcs.add(pc)
        
        # Создаем видео трек для сессии
        video_track = ProcessedVideoTrack(session_id)
        self.video_tracks[session_id] = video_track
        
        # Добавляем трек к соединению
        pc.addTrack(video_track)
        
        # Обрабатываем offer
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        
        # Создаем answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        
        return pc.localDescription.sdp
    
    def add_processed_frame(self, session_id: str, frame: np.ndarray):
        """Добавление обработанного кадра в трек"""
        if session_id in self.video_tracks:
            self.video_tracks[session_id].add_frame(frame)
    
    def set_processing_result(self, session_id: str, result: Dict):
        """Установка результата обработки"""
        if session_id in self.video_tracks:
            self.video_tracks[session_id].set_result(result)
    
    async def close_connection(self, session_id: str):
        """Закрытие соединения"""
        if session_id in self.video_tracks:
            del self.video_tracks[session_id]
        
        # Закрываем все соединения
        for pc in self.pcs:
            await pc.close()
        self.pcs.clear()

# Глобальный обработчик WebRTC
webrtc_handler = WebRTCHandler()

@app.get("/")
async def root():
    """Главная страница с WebRTC клиентом"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>NeiroFitnessApp WebRTC</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            .container { max-width: 800px; margin: 0 auto; }
            video { width: 100%; max-width: 640px; height: auto; }
            .controls { margin: 20px 0; }
            button { padding: 10px 20px; margin: 5px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
            button:hover { background: #0056b3; }
            button:disabled { background: #ccc; cursor: not-allowed; }
            .status { margin: 10px 0; padding: 10px; border-radius: 5px; }
            .status.processing { background: #fff3cd; border: 1px solid #ffeaa7; }
            .status.completed { background: #d4edda; border: 1px solid #c3e6cb; }
            .status.failed { background: #f8d7da; border: 1px solid #f5c6cb; }
            .result { margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 5px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>NeiroFitnessApp WebRTC Streaming</h1>
            
            <div class="controls">
                <button id="startBtn" onclick="startStream()">Начать стрим</button>
                <button id="stopBtn" onclick="stopStream()" disabled>Остановить стрим</button>
                <button id="processBtn" onclick="processVideo()" disabled>Обработать видео</button>
            </div>
            
            <div id="status" class="status" style="display: none;"></div>
            
            <video id="localVideo" autoplay muted playsinline></video>
            <video id="remoteVideo" autoplay playsinline></video>
            
            <div id="result" class="result" style="display: none;"></div>
        </div>

        <script>
            let localStream = null;
            let pc = null;
            let sessionId = null;
            let ws = null;

            async function startStream() {
                try {
                    // Получаем доступ к камере
                    localStream = await navigator.mediaDevices.getUserMedia({
                        video: { width: 640, height: 480 },
                        audio: false
                    });
                    
                    document.getElementById('localVideo').srcObject = localStream;
                    document.getElementById('startBtn').disabled = true;
                    document.getElementById('stopBtn').disabled = false;
                    document.getElementById('processBtn').disabled = false;
                    
                    showStatus('Стрим начат', 'processing');
                } catch (error) {
                    console.error('Ошибка доступа к камере:', error);
                    showStatus('Ошибка доступа к камере: ' + error.message, 'failed');
                }
            }

            async function stopStream() {
                if (localStream) {
                    localStream.getTracks().forEach(track => track.stop());
                    localStream = null;
                }
                
                if (pc) {
                    pc.close();
                    pc = null;
                }
                
                if (ws) {
                    ws.close();
                    ws = null;
                }
                
                document.getElementById('localVideo').srcObject = null;
                document.getElementById('remoteVideo').srcObject = null;
                document.getElementById('startBtn').disabled = false;
                document.getElementById('stopBtn').disabled = true;
                document.getElementById('processBtn').disabled = true;
                
                showStatus('Стрим остановлен', 'completed');
            }

            async function processVideo() {
                if (!localStream) {
                    showStatus('Сначала начните стрим', 'failed');
                    return;
                }

                try {
                    // Создаем WebSocket соединение
                    sessionId = generateSessionId();
                    ws = new WebSocket(`ws://localhost:8000/ws/${sessionId}`);
                    
                    ws.onopen = async () => {
                        showStatus('Подключение установлено, начинаем обработку...', 'processing');
                        
                        // Создаем RTCPeerConnection
                        pc = new RTCPeerConnection({
                            iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
                        });
                        
                        // Добавляем локальный поток
                        localStream.getTracks().forEach(track => {
                            pc.addTrack(track, localStream);
                        });
                        
                        // Обрабатываем удаленный поток
                        pc.ontrack = (event) => {
                            document.getElementById('remoteVideo').srcObject = event.streams[0];
                        };
                        
                        // Создаем offer
                        const offer = await pc.createOffer();
                        await pc.setLocalDescription(offer);
                        
                        // Отправляем offer через WebSocket
                        ws.send(JSON.stringify({
                            type: 'offer',
                            sdp: offer.sdp
                        }));
                    };
                    
                    ws.onmessage = async (event) => {
                        const data = JSON.parse(event.data);
                        
                        if (data.type === 'answer') {
                            await pc.setRemoteDescription(new RTCSessionDescription({
                                type: 'answer',
                                sdp: data.sdp
                            }));
                        } else if (data.type === 'result') {
                            showResult(data.result);
                        } else if (data.type === 'status') {
                            showStatus(data.message, data.status);
                        }
                    };
                    
                    ws.onerror = (error) => {
                        console.error('WebSocket ошибка:', error);
                        showStatus('Ошибка WebSocket соединения', 'failed');
                    };
                    
                } catch (error) {
                    console.error('Ошибка обработки видео:', error);
                    showStatus('Ошибка обработки видео: ' + error.message, 'failed');
                }
            }

            function showStatus(message, status) {
                const statusDiv = document.getElementById('status');
                statusDiv.textContent = message;
                statusDiv.className = `status ${status}`;
                statusDiv.style.display = 'block';
            }

            function showResult(result) {
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = `
                    <h3>Результат обработки:</h3>
                    <p><strong>Повторения:</strong> ${result.reps || 'N/A'}</p>
                    <p><strong>Скорость:</strong> ${result.velocity || 'N/A'}</p>
                    <p><strong>Точность:</strong> ${result.bar_path_accuracy_percent || 'N/A'}%</p>
                    <p><strong>Путь штанги:</strong> ${result.bar_path || 'N/A'}</p>
                    <p><strong>Усталость:</strong> ${result.fatigue || 'N/A'}</p>
                    <p><strong>Время под напряжением:</strong> ${result.tut || 'N/A'} сек</p>
                `;
                resultDiv.style.display = 'block';
            }

            function generateSessionId() {
                return 'session_' + Math.random().toString(36).substr(2, 9);
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint для WebRTC сигналинга"""
    await websocket.accept()
    active_connections[session_id] = websocket
    
    # Создаем сессию
    stream_sessions[session_id] = StreamSession(
        session_id=session_id,
        client_id=session_id,
        created_at=datetime.now()
    )
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message["type"] == "offer":
                # Обрабатываем WebRTC offer
                answer_sdp = await webrtc_handler.handle_offer(message["sdp"], session_id)
                
                # Отправляем answer
                await websocket.send_text(json.dumps({
                    "type": "answer",
                    "sdp": answer_sdp
                }))
                
                # Отправляем статус
                await websocket.send_text(json.dumps({
                    "type": "status",
                    "message": "WebRTC соединение установлено",
                    "status": "processing"
                }))
                
            elif message["type"] == "process":
                # Запускаем обработку видео
                await process_video_stream(session_id)
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket соединение закрыто для сессии {session_id}")
    except Exception as e:
        logger.error(f"Ошибка WebSocket для сессии {session_id}: {e}")
    finally:
        # Очистка
        if session_id in active_connections:
            del active_connections[session_id]
        if session_id in stream_sessions:
            del stream_sessions[session_id]
        await webrtc_handler.close_connection(session_id)

async def process_video_stream(session_id: str):
    """Асинхронная обработка видео потока"""
    try:
        # Создаем временный файл для записи видео
        temp_video_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
        
        # Здесь должна быть логика записи видео из WebRTC потока
        # Для демонстрации создаем заглушку
        logger.info(f"Начинаем обработку видео для сессии {session_id}")
        
        # Запускаем обработку в отдельном потоке
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            process_video_stream_sync,
            session_id,
            temp_video_path
        )
        
        # Отправляем результат через WebSocket
        if session_id in active_connections and session_id in stream_sessions:
            session = stream_sessions[session_id]
            if session.status == "completed" and session.result:
                await active_connections[session_id].send_text(json.dumps({
                    "type": "result",
                    "result": session.result
                }))
            elif session.status == "failed":
                await active_connections[session_id].send_text(json.dumps({
                    "type": "status",
                    "message": f"Ошибка обработки: {session.error_message}",
                    "status": "failed"
                }))
        
    except Exception as e:
        logger.error(f"Ошибка асинхронной обработки для сессии {session_id}: {e}")
        if session_id in active_connections:
            await active_connections[session_id].send_text(json.dumps({
                "type": "status",
                "message": f"Ошибка обработки: {str(e)}",
                "status": "failed"
            }))

def process_video_stream_sync(session_id: str, video_path: str):
    """Синхронная обработка видео потока"""
    try:
        logger.info(f"Начинаем обработку видео для сессии {session_id}")
        
        # Обновляем статус сессии
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "processing"
        
        # Обрабатываем видео используя существующую логику
        output_path = os.path.join(Config.TEMP_DIR, f"output_{session_id}.mp4")
        result, processed_path = process_video(video_path, output_path)
        
        # Перемещаем результат в выходную директорию
        final_output = os.path.join(Config.OUTPUT_DIR, f"{session_id}_processed.mp4")
        os.rename(processed_path, final_output)
        
        # Обновляем статус сессии
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "completed"
            stream_sessions[session_id].completed_at = datetime.now()
            stream_sessions[session_id].result = result
            stream_sessions[session_id].output_file = final_output
        
        logger.info(f"Обработка завершена для сессии {session_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке видео для сессии {session_id}: {e}")
        if session_id in stream_sessions:
            stream_sessions[session_id].status = "failed"
            stream_sessions[session_id].error_message = str(e)
            stream_sessions[session_id].completed_at = datetime.now()

@app.get("/api/v1/sessions")
async def get_sessions():
    """Получить все активные сессии"""
    return {"sessions": list(stream_sessions.values())}

@app.get("/api/v1/session/{session_id}")
async def get_session(session_id: str):
    """Получить информацию о сессии"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    return stream_sessions[session_id]

@app.get("/api/v1/session/{session_id}/result")
async def get_session_result(session_id: str):
    """Получить результат обработки сессии"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    session = stream_sessions[session_id]
    if session.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"session_id": session_id, "result": session.result}

@app.get("/api/v1/session/{session_id}/download")
async def download_processed_video(session_id: str):
    """Скачать обработанное видео"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    session = stream_sessions[session_id]
    if session.status != "completed" or not session.output_file:
        raise HTTPException(status_code=400, detail="Видео еще не готово")
    
    if not os.path.exists(session.output_file):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    from fastapi.responses import FileResponse
    return FileResponse(
        session.output_file,
        media_type="video/mp4",
        filename=f"processed_{session_id}.mp4"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
