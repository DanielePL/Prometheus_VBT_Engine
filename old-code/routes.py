"""
API маршруты для NeiroFitnessApp
"""
import asyncio
import os
import uuid
import json
import logging
from typing import Dict
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from app.config import Config
from app.models import JobStatus, ChunkUploadResponse, UploadSession, StreamSession
from app.utils import (
    cleanup_temp_files, assemble_file_from_chunks, cleanup_upload_session,
    process_single_video, process_video_stream
)
from app.webrtc_handler import WebRTCHandler

logger = logging.getLogger(__name__)

# Создаем роутеры
job_router = APIRouter(prefix="/api/v1", tags=["jobs"])
upload_router = APIRouter(prefix="/api/v1/upload", tags=["upload"])
webrtc_router = APIRouter(prefix="/api/v1", tags=["webrtc"])
main_router = APIRouter(tags=["main"])


# Job маршруты
@job_router.get("/job/{job_id}")
async def get_job_status(job_id: str, jobs: Dict[str, JobStatus]):
    """Получить статус задачи"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return jobs[job_id]


@job_router.get("/jobs")
async def get_all_jobs(jobs: Dict[str, JobStatus]):
    """Получить все задачи"""
    return {"jobs": list(jobs.values())}


@job_router.get("/result/{job_id}")
async def get_job_result(job_id: str, jobs: Dict[str, JobStatus]):
    """Получить результат обработки"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"job_id": job_id, "result": job.result}


@job_router.get("/download/{job_id}")
async def download_processed_video(job_id: str, jobs: Dict[str, JobStatus]):
    """Скачать обработанное видео"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    if job.status != "completed" or not job.output_file:
        raise HTTPException(status_code=400, detail="Видео еще не готово")
    
    if not os.path.exists(job.output_file):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        job.output_file,
        media_type="video/mp4",
        filename=f"processed_{job_id}.mp4"
    )


@job_router.delete("/job/{job_id}")
async def delete_job(job_id: str, jobs: Dict[str, JobStatus]):
    """Удалить задачу и связанные файлы"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    
    # Удаляем файлы
    temp_files = [
        os.path.join(Config.TEMP_DIR, f"input_{job_id}.mp4"),
        os.path.join(Config.TEMP_DIR, f"output_{job_id}.mp4"),
    ]
    
    if job.output_file and os.path.exists(job.output_file):
        temp_files.append(job.output_file)
    
    await cleanup_temp_files(temp_files)
    
    # Удаляем задачу
    del jobs[job_id]
    
    return {"message": "Задача удалена"}


@job_router.get("/stats")
async def get_system_stats(
    jobs: Dict[str, JobStatus], 
    upload_sessions: Dict[str, UploadSession],
    stream_sessions: Dict[str, StreamSession],
    active_jobs: int
):
    """Получить статистику системы"""
    total_jobs = len(jobs)
    completed_jobs = sum(1 for job in jobs.values() if job.status == "completed")
    failed_jobs = sum(1 for job in jobs.values() if job.status == "failed")
    processing_jobs = sum(1 for job in jobs.values() if job.status == "processing")
    active_uploads = len(upload_sessions)
    
    return {
        "total_jobs": total_jobs,
        "completed_jobs": completed_jobs,
        "failed_jobs": failed_jobs,
        "processing_jobs": processing_jobs,
        "active_jobs": active_jobs,
        "active_uploads": active_uploads,
        "max_concurrent_jobs": Config.MAX_CONCURRENT_JOBS,
        "max_file_size_mb": Config.get_max_file_size_mb()
    }


# Upload маршруты
@upload_router.post("/init")
async def init_chunked_upload(
    filename: str = Form(...),
    total_size: int = Form(...),
    total_chunks: int = Form(...),
    upload_sessions: Dict[str, UploadSession] = None
):
    """Инициализация chunked upload"""
    # Валидация параметров
    if total_size > Config.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Файл слишком большой: {total_size / (1024*1024):.1f}MB (максимум {Config.get_max_file_size_mb():.1f}MB)"
        )
    
    if total_chunks > 1000:  # Разумное ограничение
        raise HTTPException(
            status_code=400,
            detail="Слишком много частей. Максимум: 1000"
        )
    
    # Создаем новую сессию загрузки
    upload_id = str(uuid.uuid4())
    upload_sessions[upload_id] = UploadSession(
        upload_id=upload_id,
        filename=filename,
        total_size=total_size,
        total_chunks=total_chunks,
        created_at=datetime.now()
    )
    
    logger.info(f"Инициализирована загрузка: {upload_id}, файл: {filename}, размер: {total_size / (1024*1024):.1f}MB, частей: {total_chunks}")
    
    return {
        "upload_id": upload_id,
        "message": "Сессия загрузки создана",
        "total_chunks": total_chunks,
        "chunk_size": Config.CHUNK_SIZE
    }


@upload_router.post("/chunk", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_number: int = Form(...),
    chunk: UploadFile = File(...),
    upload_sessions: Dict[str, UploadSession] = None
):
    """Загрузка части файла"""
    # Проверяем существование сессии
    if upload_id not in upload_sessions:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена")
    
    session = upload_sessions[upload_id]
    
    # Проверяем статус сессии
    if session.status != "uploading":
        raise HTTPException(status_code=400, detail=f"Сессия в статусе: {session.status}")
    
    # Валидация номера части
    if chunk_number < 1 or chunk_number > session.total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"Неверный номер части: {chunk_number}. Должен быть от 1 до {session.total_chunks}"
        )
    
    # Проверяем, не загружена ли уже эта часть
    if chunk_number in session.chunks:
        raise HTTPException(status_code=400, detail=f"Часть {chunk_number} уже загружена")
    
    try:
        # Сохраняем часть
        chunk_path = os.path.join(Config.TEMP_DIR, f"chunk_{upload_id}_{chunk_number}")
        
        # Читаем все данные из загруженной части
        chunk_content = await chunk.read()
        
        with open(chunk_path, "wb") as f:
            f.write(chunk_content)
        
        logger.info(f"Сохранена часть {chunk_number}, размер данных: {len(chunk_content)} байт")
        
        # Получаем размер загруженной части
        chunk_size = os.path.getsize(chunk_path)
        
        # Обновляем сессию
        session.chunks[chunk_number] = chunk_path
        session.uploaded_chunks += 1
        
        # Пересчитываем общий размер заново (избегаем накопления ошибок)
        session.uploaded_size = sum(os.path.getsize(path) for path in session.chunks.values())
        
        logger.info(f"Загружена часть {chunk_number}/{session.total_chunks} для {upload_id}, размер части: {chunk_size} байт, общий размер: {session.uploaded_size} байт")
        
        return ChunkUploadResponse(
            upload_id=upload_id,
            chunk_number=chunk_number,
            total_chunks=session.total_chunks,
            uploaded_size=session.uploaded_size,
            message=f"Часть {chunk_number} загружена успешно"
        )
        
    except Exception as e:
        logger.error(f"Ошибка при загрузке части {chunk_number}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@upload_router.post("/complete")
async def complete_chunked_upload(
    background_tasks: BackgroundTasks,
    upload_id: str = Form(...),
    upload_sessions: Dict[str, UploadSession] = None,
    jobs: Dict[str, JobStatus] = None,
    active_jobs_ref = None,
    job_semaphore = None,
    thread_pool = None
):
    """Завершение chunked upload и запуск обработки"""
    # Проверяем существование сессии
    if upload_id not in upload_sessions:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена")
    
    session = upload_sessions[upload_id]
    
    # Проверяем, что все части загружены
    if session.uploaded_chunks != session.total_chunks:
        missing_chunks = [i for i in range(1, session.total_chunks + 1) if i not in session.chunks]
        raise HTTPException(
            status_code=400,
            detail=f"Не все части загружены. Отсутствуют: {missing_chunks}"
        )
    
    try:
        # Собираем файл из частей
        logger.info(f"Начинаем сборку файла для {upload_id}")
        assembled_file = await assemble_file_from_chunks(session)
        
        # Создаем задачу обработки
        job_id = str(uuid.uuid4())
        temp_output = os.path.join(Config.TEMP_DIR, f"output_{job_id}.mp4")
        
        jobs[job_id] = JobStatus(
            job_id=job_id,
            status="pending",
            created_at=datetime.now()
        )
        
        # Запускаем обработку в фоне
        async with job_semaphore:
            active_jobs_ref[0] += 1
            background_tasks.add_task(
                process_single_video,
                job_id,
                assembled_file,
                temp_output,
                jobs,
                active_jobs_ref,
                thread_pool
            )
        
        # Обновляем статус сессии
        session.status = "completed"
        session.completed_at = datetime.now()
        
        # Планируем очистку через 1 час
        background_tasks.add_task(
            asyncio.sleep,
            3600
        )
        background_tasks.add_task(
            cleanup_upload_session,
            upload_id,
            upload_sessions
        )
        
        logger.info(f"Chunked upload завершен для {upload_id}, создана задача {job_id}")
        
        return {
            "upload_id": upload_id,
            "job_id": job_id,
            "message": "Файл собран и обработка начата",
            "filename": session.filename,
            "total_size": session.total_size
        }
        
    except Exception as e:
        session.status = "failed"
        logger.error(f"Ошибка при завершении загрузки {upload_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@upload_router.get("/{upload_id}")
async def get_upload_status(upload_id: str, upload_sessions: Dict[str, UploadSession]):
    """Получить статус загрузки"""
    if upload_id not in upload_sessions:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена")
    
    session = upload_sessions[upload_id]
    
    return {
        "upload_id": upload_id,
        "filename": session.filename,
        "status": session.status,
        "total_size": session.total_size,
        "uploaded_size": session.uploaded_size,
        "total_chunks": session.total_chunks,
        "uploaded_chunks": session.uploaded_chunks,
        "progress_percent": round((session.uploaded_chunks / session.total_chunks) * 100, 2),
        "created_at": session.created_at,
        "completed_at": session.completed_at
    }


@upload_router.delete("/{upload_id}")
async def cancel_upload(upload_id: str, upload_sessions: Dict[str, UploadSession]):
    """Отменить загрузку"""
    if upload_id not in upload_sessions:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена")
    
    await cleanup_upload_session(upload_id, upload_sessions)
    
    return {"message": "Загрузка отменена"}


# WebRTC маршруты
@webrtc_router.get("/webrtc")
async def webrtc_page():
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


@webrtc_router.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket, 
    session_id: str,
    active_connections: Dict[str, WebSocket] = None,
    stream_sessions: Dict[str, StreamSession] = None,
    webrtc_handler: WebRTCHandler = None
):
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
                await process_video_stream(session_id, stream_sessions, active_connections)
                
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


@webrtc_router.get("/sessions")
async def get_sessions(stream_sessions: Dict[str, StreamSession]):
    """Получить все активные сессии"""
    return {"sessions": list(stream_sessions.values())}


@webrtc_router.get("/session/{session_id}")
async def get_session(session_id: str, stream_sessions: Dict[str, StreamSession]):
    """Получить информацию о сессии"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    return stream_sessions[session_id]


@webrtc_router.get("/session/{session_id}/result")
async def get_session_result(session_id: str, stream_sessions: Dict[str, StreamSession]):
    """Получить результат обработки сессии"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    session = stream_sessions[session_id]
    if session.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"session_id": session_id, "result": session.result}


@webrtc_router.get("/session/{session_id}/download")
async def download_processed_video(session_id: str, stream_sessions: Dict[str, StreamSession]):
    """Скачать обработанное видео"""
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    session = stream_sessions[session_id]
    if session.status != "completed" or not session.output_file:
        raise HTTPException(status_code=400, detail="Видео еще не готово")
    
    if not os.path.exists(session.output_file):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        session.output_file,
        media_type="video/mp4",
        filename=f"processed_{session_id}.mp4"
    )


# Главный маршрут
@main_router.get("/")
async def root():
    """Корневой эндпоинт"""
    return {
        "message": "NeiroFitnessApp API - Unified Video Processing",
        "version": "1.0.0",
        "features": [
            "Chunked upload для больших файлов",
            "WebRTC live streaming",
            "Загрузка видео по частям",
            "Автоматическая сборка файлов",
            "Отслеживание прогресса",
            "Защита от перегрузки системы",
            "Real-time video processing"
        ],
        "endpoints": {
            "chunked_upload": {
                "init": "/api/v1/upload/init",
                "chunk": "/api/v1/upload/chunk", 
                "complete": "/api/v1/upload/complete",
                "status": "/api/v1/upload/{upload_id}",
                "cancel": "/api/v1/upload/{upload_id}"
            },
            "webrtc_streaming": {
                "page": "/webrtc",
                "websocket": "/ws/{session_id}",
                "sessions": "/api/v1/sessions",
                "session_info": "/api/v1/session/{session_id}",
                "session_result": "/api/v1/session/{session_id}/result",
                "session_download": "/api/v1/session/{session_id}/download"
            },
            "job_management": {
                "status": "/api/v1/job/{job_id}",
                "download": "/api/v1/download/{job_id}",
                "result": "/api/v1/result/{job_id}",
                "delete": "/api/v1/job/{job_id}"
            },
            "system": {
                "stats": "/api/v1/stats",
                "all_jobs": "/api/v1/jobs"
            }
        }
    }
