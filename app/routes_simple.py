"""
Упрощенные API маршруты для NeiroFitnessApp
"""
import asyncio
import os
import uuid
import json
import logging
from typing import Dict
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Form, WebSocket, WebSocketDisconnect, Request
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
async def get_job_status(job_id: str, request: Request):
    """Получить статус задачи"""
    jobs = request.app.state.jobs
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return jobs[job_id]


@job_router.get("/jobs")
async def get_all_jobs(request: Request):
    """Получить все задачи"""
    jobs = request.app.state.jobs
    return {"jobs": list(jobs.values())}


@job_router.get("/result/{job_id}")
async def get_job_result(job_id: str, request: Request):
    """Получить результат обработки"""
    jobs = request.app.state.jobs
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"job_id": job_id, "result": job.result}


@job_router.delete("/job/{job_id}")
async def delete_job(job_id: str, request: Request):
    """Удалить задачу и связанные файлы"""
    jobs = request.app.state.jobs
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
async def get_system_stats(request: Request):
    """Получить статистику системы"""
    jobs = request.app.state.jobs
    upload_sessions = request.app.state.upload_sessions
    stream_sessions = request.app.state.stream_sessions
    active_jobs = request.app.state.active_jobs[0]
    
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
    request: Request = None
):
    """Инициализация chunked upload"""
    upload_sessions = request.app.state.upload_sessions
    
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
    request: Request = None
):
    """Загрузка части файла"""
    upload_sessions = request.app.state.upload_sessions
    
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
    request: Request = None
):
    """Завершение chunked upload и запуск обработки"""
    upload_sessions = request.app.state.upload_sessions
    jobs = request.app.state.jobs
    active_jobs = request.app.state.active_jobs
    job_semaphore = request.app.state.job_semaphore
    thread_pool = request.app.state.thread_pool
    
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
            active_jobs[0] += 1
            background_tasks.add_task(
                process_single_video,
                job_id,
                assembled_file,
                temp_output,
                jobs,
                active_jobs,
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
async def get_upload_status(upload_id: str, request: Request):
    """Получить статус загрузки"""
    upload_sessions = request.app.state.upload_sessions
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
async def cancel_upload(upload_id: str, request: Request):
    """Отменить загрузку"""
    upload_sessions = request.app.state.upload_sessions
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
            .progress { margin: 10px 0; height: 16px; background: #eee; border-radius: 8px; overflow: hidden; border: 1px solid #ddd; }
            .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #28a745, #17a2b8); transition: width .3s ease; }
            .muted { color: #666; font-size: 12px; }
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

            <div id="progressWrap" style="display: none;">
                <div class="muted" id="jobInfo"></div>
                <div class="progress"><div id="progressBar" class="progress-bar"></div></div>
                <div class="muted" id="progressText">0%</div>
            </div>
        </div>

        <script>
            let localStream = null;
            let pc = null;
            let sessionId = null;
            let ws = null;
            let jobId = null;
            let pollTimer = null;

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

                    // Создаем WebSocket и RTCPeerConnection сразу при старте, чтобы начать запись на сервере
                    sessionId = generateSessionId();
                    const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
                    const wsBase = `${wsProtocol}://${location.host}`;
                    ws = new WebSocket(`${wsBase}/api/v1/ws/${sessionId}`);

                    ws.onopen = async () => {
                        try {
                            pc = new RTCPeerConnection({
                                iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
                            });

                            localStream.getTracks().forEach(track => {
                                pc.addTrack(track, localStream);
                            });

                            pc.ontrack = (event) => {
                                document.getElementById('remoteVideo').srcObject = event.streams[0];
                            };

                            const offer = await pc.createOffer();
                            await pc.setLocalDescription(offer);

                            // Ждём пока ICE-gathering завершится, чтобы sdp включал кандидатов
                            await new Promise(resolve => {
                                if (pc.iceGatheringState === 'complete') {
                                    resolve();
                                } else {
                                    const checkState = () => {
                                        if (pc.iceGatheringState === 'complete') {
                                            pc.removeEventListener('icegatheringstatechange', checkState);
                                            resolve();
                                        }
                                    };
                                    pc.addEventListener('icegatheringstatechange', checkState);
                                }
                            });

                            ws.send(JSON.stringify({
                                type: 'offer',
                                sdp: pc.localDescription.sdp
                            }));
                        } catch (e) {
                            console.error('Ошибка инициализации WebRTC:', e);
                            showStatus('Ошибка инициализации WebRTC', 'failed');
                        }
                    };

                    ws.onmessage = async (event) => {
                        const data = JSON.parse(event.data);
                        if (!pc) return;
                        if (data.type === 'answer') {
                            try {
                                await pc.setRemoteDescription(new RTCSessionDescription({
                                    type: 'answer',
                                    sdp: data.sdp
                                }));
                            } catch (e) {
                                console.error('Ошибка установки answer:', e);
                            }
                        } else if (data.type === 'result') {
                            showResult(data.result);
                        } else if (data.type === 'status') {
                            showStatus(data.message, data.status);
                        } else if (data.type === 'processing_started') {
                            jobId = data.job_id;
                            showStatus(`Обработка начата. job_id: ${jobId}`, 'processing');
                            showProgress(0, jobId);
                            startJobPolling(jobId);
                        }
                    };

                    ws.onerror = (error) => {
                        console.error('WebSocket ошибка:', error);
                        showStatus('Ошибка WebSocket соединения', 'failed');
                    };

                    ws.onclose = () => {
                        showStatus('Соединение закрыто', 'completed');
                    };
                } catch (error) {
                    console.error('Ошибка доступа к камере:', error);
                    showStatus('Ошибка доступа к камере: ' + error.message, 'failed');
                }
            }

            async function stopStream() {
                try {
                    // Попросим сервер остановить запись и запустить обработку
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        const waitForAck = new Promise((resolve) => {
                            const handler = (event) => {
                                try {
                                    const data = JSON.parse(event.data);
                                    if (data.type === 'processing_started') {
                                        showStatus(`Обработка начата. job_id: ${data.job_id}`, 'processing');
                                        ws.removeEventListener('message', handler);
                                        resolve();
                                    }
                                } catch {}
                            };
                            ws.addEventListener('message', handler);
                        });
                        ws.send(JSON.stringify({ type: 'stop' }));
                        // Ждем подтверждение запуска обработки (до 1.5с)
                        await Promise.race([
                            waitForAck,
                            new Promise(r => setTimeout(r, 1500))
                        ]);
                    }
                } catch (e) {
                    console.error('Ошибка при остановке стрима:', e);
                } finally {
                    if (localStream) {
                        localStream.getTracks().forEach(track => track.stop());
                        localStream = null;
                    }
                    if (pc) {
                        try { pc.close(); } catch {}
                        pc = null;
                    }
                    if (ws) {
                        try { ws.close(); } catch {}
                        ws = null;
                    }
                    document.getElementById('localVideo').srcObject = null;
                    document.getElementById('remoteVideo').srcObject = null;
                    document.getElementById('startBtn').disabled = false;
                    document.getElementById('stopBtn').disabled = true;
                    document.getElementById('processBtn').disabled = true;
                    showStatus('Стрим остановлен', 'completed');
                }
            }

            async function processVideo() {
                if (!localStream) {
                    showStatus('Сначала начните стрим', 'failed');
                    return;
                }

                try {
                    // Если соединение уже открыто, просто отправим команду обработки
                    if (!ws || ws.readyState !== WebSocket.OPEN) {
                        showStatus('WebSocket не готов. Сначала начните стрим.', 'failed');
                        return;
                    }
                    showStatus('Запросили обработку...', 'processing');
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
                        } else if (data.type === 'processing_started') {
                            jobId = data.job_id;
                            showStatus(`Обработка начата. job_id: ${jobId}`, 'processing');
                            showProgress(0, jobId);
                            startJobPolling(jobId);
                        }
                    };
                    // Отправим запрос на обработку текущей записи (опционально)
                    ws.send(JSON.stringify({ type: 'process' }));

                    // Fallback: если сервер не прислал job_id через WS, пытаемся получить через REST
                    setTimeout(async () => {
                        if (!jobId && sessionId) {
                            try {
                                const resp = await fetch(`/api/v1/session/${sessionId}/job`);
                                if (resp.ok) {
                                    const j = await resp.json();
                                    if (j.job_id) {
                                        jobId = j.job_id;
                                        showStatus(`Обработка начата. job_id: ${jobId}`, 'processing');
                                        showProgress(0, jobId);
                                        startJobPolling(jobId);
                                    }
                                }
                            } catch {}
                        }
                    }, 1500);
                    
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
                showProgress(100, jobId);
            }

            function generateSessionId() {
                return 'session_' + Math.random().toString(36).substr(2, 9);
            }

            function showProgress(percent, id) {
                const wrap = document.getElementById('progressWrap');
                const bar = document.getElementById('progressBar');
                const text = document.getElementById('progressText');
                const info = document.getElementById('jobInfo');
                wrap.style.display = 'block';
                bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
                text.textContent = `${Math.round(Math.max(0, Math.min(100, percent)))}%`;
                info.textContent = id ? `job_id: ${id}` : '';
            }

            function startJobPolling(id) {
                if (pollTimer) clearInterval(pollTimer);
                pollTimer = setInterval(async () => {
                    try {
                        const res = await fetch(`/api/v1/job/${id}`);
                        if (!res.ok) return;
                        const job = await res.json();
                        if ('progress' in job) {
                            showProgress(job.progress || 0, id);
                        }
                        if (job.status === 'completed') {
                            clearInterval(pollTimer);
                            showProgress(100, id);
                            try {
                                const rr = await fetch(`/api/v1/result/${id}`);
                                if (rr.ok) {
                                    const data = await rr.json();
                                    if (data && data.result) {
                                        showResult(data.result);
                                    }
                                }
                            } catch {}
                            showStatus('Обработка завершена', 'completed');
                        } else if (job.status === 'failed') {
                            clearInterval(pollTimer);
                            showStatus(job.error_message || 'Ошибка обработки', 'failed');
                        }
                    } catch (e) {
                        console.error('Ошибка запроса статуса job:', e);
                    }
                }, 1500);
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@webrtc_router.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket, 
    session_id: str
):
    """WebSocket endpoint для WebRTC сигналинга"""
    app = getattr(websocket, "app", None) or websocket.scope.get("app")
    active_connections = app.state.active_connections
    stream_sessions = app.state.stream_sessions
    webrtc_handler = app.state.webrtc_handler
    jobs = app.state.jobs
    active_jobs = app.state.active_jobs
    job_semaphore = app.state.job_semaphore
    thread_pool = app.state.thread_pool
    
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
                # Явный запрос на обработку записанного видео
                input_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
                if not os.path.exists(input_path):
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": "Нет записанного видео для обработки",
                        "status": "failed"
                    }))
                    continue
                job_id = str(uuid.uuid4())
                temp_output = os.path.join(Config.TEMP_DIR, f"output_{job_id}.mp4")
                jobs[job_id] = JobStatus(
                    job_id=job_id,
                    status="pending",
                    created_at=datetime.now()
                )
                if session_id in stream_sessions:
                    stream_sessions[session_id].status = "processing"
                    stream_sessions[session_id].job_id = job_id
                async def _schedule():
                    async with job_semaphore:
                        active_jobs[0] += 1
                        await process_single_video(job_id, input_path, temp_output, jobs, active_jobs, thread_pool)
                        job = jobs.get(job_id)
                        if session_id in stream_sessions and job is not None:
                            if job.status == "completed":
                                stream_sessions[session_id].status = "completed"
                                stream_sessions[session_id].completed_at = datetime.now()
                                stream_sessions[session_id].result = job.result
                                stream_sessions[session_id].output_file = job.output_file
                            elif job.status == "failed":
                                stream_sessions[session_id].status = "failed"
                                stream_sessions[session_id].completed_at = datetime.now()
                                stream_sessions[session_id].error_message = job.error_message
                asyncio.create_task(_schedule())
                await websocket.send_text(json.dumps({
                    "type": "processing_started",
                    "session_id": session_id,
                    "job_id": job_id
                }))
            elif message["type"] == "stop":
                # Останавливаем запись, создаём задачу обработки и возвращаем job_id
                try:
                    await webrtc_handler.close_connection(session_id)
                except Exception as e:
                    logger.error(f"Ошибка при остановке записи {session_id}: {e}")
                input_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
                if not os.path.exists(input_path):
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": "Видео не записано",
                        "status": "failed"
                    }))
                    continue
                job_id = str(uuid.uuid4())
                temp_output = os.path.join(Config.TEMP_DIR, f"output_{job_id}.mp4")
                jobs[job_id] = JobStatus(
                    job_id=job_id,
                    status="pending",
                    created_at=datetime.now()
                )
                if session_id in stream_sessions:
                    stream_sessions[session_id].status = "processing"
                    stream_sessions[session_id].job_id = job_id
                async def _schedule2():
                    async with job_semaphore:
                        active_jobs[0] += 1
                        await process_single_video(job_id, input_path, temp_output, jobs, active_jobs, thread_pool)
                        job = jobs.get(job_id)
                        if session_id in stream_sessions and job is not None:
                            if job.status == "completed":
                                stream_sessions[session_id].status = "completed"
                                stream_sessions[session_id].completed_at = datetime.now()
                                stream_sessions[session_id].result = job.result
                                stream_sessions[session_id].output_file = job.output_file
                            elif job.status == "failed":
                                stream_sessions[session_id].status = "failed"
                                stream_sessions[session_id].completed_at = datetime.now()
                                stream_sessions[session_id].error_message = job.error_message
                asyncio.create_task(_schedule2())
                await websocket.send_text(json.dumps({
                    "type": "processing_started",
                    "session_id": session_id,
                    "job_id": job_id
                }))
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket соединение закрыто для сессии {session_id}")
    except Exception as e:
        logger.error(f"Ошибка WebSocket для сессии {session_id}: {e}")
    finally:
        # Очистка: закрываем соединение и останавливаем запись
        if session_id in active_connections:
            del active_connections[session_id]
        # Не удаляем stream_sessions, чтобы можно было получить результат после обработки
        try:
            await webrtc_handler.close_connection(session_id)
        except Exception:
            pass


@webrtc_router.get("/sessions")
async def get_sessions(request: Request):
    """Получить все активные сессии"""
    stream_sessions = request.app.state.stream_sessions
    return {"sessions": list(stream_sessions.values())}


@webrtc_router.get("/session/{session_id}")
async def get_session(session_id: str, request: Request):
    """Получить информацию о сессии"""
    stream_sessions = request.app.state.stream_sessions
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    return stream_sessions[session_id]


@webrtc_router.get("/session/{session_id}/result")
async def get_session_result(session_id: str, request: Request):
    """Получить результат обработки сессии"""
    stream_sessions = request.app.state.stream_sessions
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    
    session = stream_sessions[session_id]
    if session.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"session_id": session_id, "result": session.result}


@webrtc_router.get("/session/{session_id}/job")
async def get_session_job(session_id: str, request: Request):
    """Получить job_id, связанный с WebRTC-сессией"""
    stream_sessions = request.app.state.stream_sessions
    if session_id not in stream_sessions:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    session = stream_sessions[session_id]
    if not session.job_id:
        raise HTTPException(status_code=400, detail="Для сессии не создана задача обработки")
    return {"session_id": session_id, "job_id": session.job_id}


@webrtc_router.get("/session/{session_id}/download")
async def download_processed_video(session_id: str, request: Request):
    """Скачать обработанное видео"""
    stream_sessions = request.app.state.stream_sessions
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
