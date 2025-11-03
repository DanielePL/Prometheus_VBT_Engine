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

@job_router.get("/download/{job_id}")
async def download_processed_video(job_id: str, request: Request):
    """Скачать обработанное видео"""
    jobs = request.app.state.jobs
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
            <div id="recordingStatus" style="display: none; padding: 10px; margin: 10px 0; background: #e8f5e9; border: 1px solid #4caf50; border-radius: 5px;">
                <strong>🔴 Запись идёт...</strong>
                <div class="muted" style="font-size: 12px; margin-top: 5px;">Трек получен и запись началась на сервере</div>
            </div>
            
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
                    console.log('🎬 Начало захвата камеры...');
                    showStatus('Получение доступа к камере...', 'processing');
                    
                    // Получаем доступ к камере
                    localStream = await navigator.mediaDevices.getUserMedia({
                        video: { width: 640, height: 480 },
                        audio: false
                    });
                    
                    console.log('✅ Камера успешно захвачена');
                    console.log('📹 Треки:', localStream.getTracks().map(t => ({ kind: t.kind, id: t.id, enabled: t.enabled })));
                    
                    document.getElementById('localVideo').srcObject = localStream;
                    document.getElementById('startBtn').disabled = true;
                    document.getElementById('stopBtn').disabled = false;
                    document.getElementById('processBtn').disabled = false;
                    
                    showStatus('Камера захвачена, устанавливается соединение...', 'processing');

                    // Создаем WebSocket и RTCPeerConnection сразу при старте, чтобы начать запись на сервере
                    sessionId = generateSessionId();
                    console.log('🆔 Session ID:', sessionId);
                    
                    const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
                    const wsBase = `${wsProtocol}://${location.host}`;
                    const wsUrl = `${wsBase}/api/v1/ws/${sessionId}`;
                    
                    console.log('🔌 Подключение к WebSocket:', wsUrl);
                    ws = new WebSocket(wsUrl);

                    ws.onopen = async () => {
                        console.log('✅ WebSocket подключен успешно');
                        showStatus('WebSocket подключен, создаётся RTCPeerConnection...', 'processing');
                        
                        try {
                            console.log('🔧 Создание RTCPeerConnection...');
                            pc = new RTCPeerConnection({
                                iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
                            });
                            console.log('✅ RTCPeerConnection создан');

                            // Диагностика состояния соединения
                            pc.onconnectionstatechange = () => {
                                console.log('🔌 Состояние соединения:', pc.connectionState);
                                
                                if (pc.connectionState === 'connected') {
                                    console.log('🎉 WebRTC соединение успешно установлено!');
                                    showStatus('WebRTC соединение установлено. Запись началась.', 'processing');
                                    // Показываем индикатор записи только при успешном соединении
                                    document.getElementById('recordingStatus').style.display = 'block';
                                } else if (pc.connectionState === 'connecting') {
                                    console.log('⏳ Устанавливается WebRTC соединение...');
                                    showStatus('Устанавливается соединение...', 'processing');
                                } else if (pc.connectionState === 'failed') {
                                    console.error('❌ WebRTC соединение не удалось установить');
                                    showStatus('WebRTC соединение не удалось. Проверьте сеть и перезагрузите страницу.', 'failed');
                                    document.getElementById('recordingStatus').style.display = 'none';
                                } else if (pc.connectionState === 'disconnected') {
                                    console.warn('⚠️ WebRTC соединение разорвано');
                                    showStatus('WebRTC соединение потеряно: ' + pc.connectionState, 'failed');
                                    document.getElementById('recordingStatus').style.display = 'none';
                                }
                            };

                            pc.oniceconnectionstatechange = () => {
                                console.log('❄️ ICE состояние:', pc.iceConnectionState);
                                
                                if (pc.iceConnectionState === 'checking') {
                                    console.log('🔍 Проверка ICE кандидатов...');
                                } else if (pc.iceConnectionState === 'connected') {
                                    console.log('✅ ICE соединение установлено');
                                } else if (pc.iceConnectionState === 'completed') {
                                    console.log('✅ ICE соединение завершено (все кандидаты проверены)');
                                } else if (pc.iceConnectionState === 'failed') {
                                    console.error('❌ ICE соединение не удалось');
                                    showStatus('Не удалось установить соединение. Проверьте сеть.', 'failed');
                                } else if (pc.iceConnectionState === 'disconnected') {
                                    console.warn('⚠️ ICE соединение потеряно');
                                }
                            };

                            // Добавляем видео треки
                            console.log('📹 Доступные треки:', localStream.getTracks().length);
                            localStream.getTracks().forEach(track => {
                                console.log('➕ Добавляем трек:', track.kind, track.id, 'enabled:', track.enabled);
                                const sender = pc.addTrack(track, localStream);
                                console.log('✅ Трек добавлен в peer connection, sender:', sender);
                            });

                            pc.ontrack = (event) => {
                                console.log('📥 Получен входящий трек от сервера:', event.track.kind);
                                document.getElementById('remoteVideo').srcObject = event.streams[0];
                            };

                            console.log('📝 Создание offer...');
                            const offer = await pc.createOffer();
                            console.log('✅ Offer создан');
                            
                            console.log('📝 Установка localDescription...');
                            await pc.setLocalDescription(offer);
                            console.log('✅ LocalDescription установлен');

                            // Ждём пока ICE-gathering завершится, чтобы sdp включал кандидатов
                            console.log('⏳ Ожидание завершения ICE gathering...');
                            await new Promise(resolve => {
                                if (pc.iceGatheringState === 'complete') {
                                    console.log('✅ ICE gathering уже завершен');
                                    resolve();
                                } else {
                                    const checkState = () => {
                                        console.log('🔄 ICE gathering состояние:', pc.iceGatheringState);
                                        if (pc.iceGatheringState === 'complete') {
                                            console.log('✅ ICE gathering завершен');
                                            pc.removeEventListener('icegatheringstatechange', checkState);
                                            resolve();
                                        }
                                    };
                                    pc.addEventListener('icegatheringstatechange', checkState);
                                }
                            });

                            console.log('📤 Отправка offer на сервер...');
                            console.log('SDP тип:', pc.localDescription.type);
                            console.log('SDP содержит треки:', pc.localDescription.sdp.includes('m=video'));
                            
                            ws.send(JSON.stringify({
                                type: 'offer',
                                sdp: pc.localDescription.sdp
                            }));
                            console.log('✅ Offer отправлен');
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
                                console.log('✅ WebRTC answer установлен успешно');
                                
                                // Ждём установления соединения (максимум 10 секунд)
                                const connectionTimeout = setTimeout(() => {
                                    if (pc && pc.connectionState !== 'connected') {
                                        console.error('❌ WebRTC соединение не установлено за 10 секунд');
                                        showStatus('WebRTC соединение не установлено. Попробуйте перезагрузить страницу.', 'failed');
                                    }
                                }, 10000);
                                
                                // Проверяем установление соединения
                                if (pc.connectionState === 'connected') {
                                    clearTimeout(connectionTimeout);
                                    console.log('🎉 WebRTC соединение установлено!');
                                }
                            } catch (e) {
                                console.error('❌ Ошибка установки answer:', e);
                                showStatus('Ошибка WebRTC: ' + e.message, 'failed');
                            }
                        } else if (data.type === 'result') {
                            showResult(data.result);
                        } else if (data.type === 'status') {
                            // Не перезаписываем статус если соединение уже установлено
                            if (pc.connectionState !== 'connected') {
                                showStatus(data.message, data.status);
                            }
                        } else if (data.type === 'processing_started') {
                            jobId = data.job_id;
                            showStatus(`Обработка начата. job_id: ${jobId}`, 'processing');
                            showProgress(0, jobId);
                            startJobPolling(jobId);
                            document.getElementById('recordingStatus').style.display = 'none';
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
                    console.error('❌ ОШИБКА при запуске стрима:', error);
                    console.error('Тип ошибки:', error.name);
                    console.error('Сообщение:', error.message);
                    
                    let errorMessage = 'Ошибка: ';
                    if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
                        errorMessage += 'Доступ к камере запрещён. Разрешите доступ в настройках браузера.';
                    } else if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
                        errorMessage += 'Камера не найдена. Подключите камеру.';
                    } else if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
                        errorMessage += 'Камера занята другим приложением. Закройте другие приложения.';
                    } else if (error.name === 'OverconstrainedError' || error.name === 'ConstraintNotSatisfiedError') {
                        errorMessage += 'Камера не поддерживает запрошенные параметры.';
                    } else {
                        errorMessage += error.message || 'Неизвестная ошибка';
                    }
                    
                    showStatus(errorMessage, 'failed');
                    
                    // Сбрасываем кнопки
                    document.getElementById('startBtn').disabled = false;
                    document.getElementById('stopBtn').disabled = true;
                    document.getElementById('processBtn').disabled = true;
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
                    document.getElementById('recordingStatus').style.display = 'none';
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
    
    logger.info(f"🔌 WebSocket подключён: session_id={session_id}")
    
    # Создаем сессию
    stream_sessions[session_id] = StreamSession(
        session_id=session_id,
        client_id=session_id,
        created_at=datetime.now()
    )
    
    logger.info(f"📝 Создана сессия стрима: {session_id}")
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            logger.info(f"📨 Получено WebSocket сообщение от {session_id}: тип={message.get('type', 'unknown')}")
            
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
                logger.info(f"Запрос обработки видео: {input_path}")
                
                if not os.path.exists(input_path):
                    logger.error(f"Видео файл не найден для обработки: {input_path}")
                    
                    error_msg = "Нет записанного видео для обработки. Возможные причины:\n"
                    error_msg += "- Запись еще не начата или не завершена\n"
                    error_msg += "- Видео трек не был получен от клиента\n"
                    error_msg += "- Нужно подождать несколько секунд после начала стрима"
                    
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": error_msg,
                        "status": "failed"
                    }))
                    continue
                
                # Проверяем размер файла
                file_size = os.path.getsize(input_path)
                logger.info(f"Обработка видео файла: {input_path}, размер: {file_size} байт")
                
                if file_size < 1024:  # Меньше 1KB - подозрительно малый файл
                    logger.warning(f"Видео файл слишком маленький для обработки: {file_size} байт")
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": f"Видео файл слишком маленький ({file_size} байт). Запишите больше видео перед обработкой.",
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
                # Проверяем, был ли вообще отправлен offer
                if session_id not in webrtc_handler.peer_connections:
                    logger.error(f"❌ Получена команда 'stop' без предварительного 'offer' для сессии {session_id}")
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": "Ошибка: WebRTC соединение не было установлено.\n\n"
                                 "Возможные причины:\n"
                                 "1. Камера не захвачена (проверьте разрешения браузера)\n"
                                 "2. Ошибка при создании WebRTC соединения\n"
                                 "3. Проблема с JavaScript в браузере\n\n"
                                 "Откройте консоль браузера (F12) и проверьте ошибки!",
                        "status": "failed"
                    }))
                    continue
                
                # Останавливаем запись, создаём задачу обработки и возвращаем job_id
                try:
                    await webrtc_handler.close_connection(session_id)
                except Exception as e:
                    logger.error(f"Ошибка при остановке записи {session_id}: {e}")
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": f"Ошибка остановки записи: {str(e)}",
                        "status": "failed"
                    }))
                    continue
                    
                input_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
                logger.info(f"Проверка наличия видео файла: {input_path}")
                
                if not os.path.exists(input_path):
                    # Проверяем причину отсутствия файла
                    was_recording = webrtc_handler.is_recording(session_id)
                    logger.error(f"Видео файл не найден: {input_path}, запись велась: {was_recording}")
                    
                    error_msg = "Видео не записано. Возможные причины:\n"
                    error_msg += "- Видео трек не был получен от клиента\n"
                    error_msg += "- Стрим был слишком коротким\n"
                    error_msg += "- Проблемы с WebRTC соединением"
                    
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": error_msg,
                        "status": "failed"
                    }))
                    continue
                
                # Проверяем размер файла
                file_size = os.path.getsize(input_path)
                logger.info(f"Видео файл найден: {input_path}, размер: {file_size} байт")
                
                if file_size < 1024:  # Меньше 1KB - подозрительно малый файл
                    logger.warning(f"Видео файл слишком маленький: {file_size} байт")
                    await websocket.send_text(json.dumps({
                        "type": "status",
                        "message": f"Видео файл слишком маленький ({file_size} байт). Возможно стрим был слишком коротким.",
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
@main_router.get("/video-upload")
async def video_upload_page():
    """Страница загрузки видео по частям"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>NeiroFitnessApp - Загрузка видео</title>
        <meta charset="UTF-8">
        <style>
            * { box-sizing: border-box; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                margin: 0;
                padding: 20px;
                background: #f5f5f5;
            }
            .container { 
                max-width: 900px; 
                margin: 0 auto;
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 { 
                color: #333;
                margin-top: 0;
                margin-bottom: 10px;
            }
            .subtitle {
                color: #666;
                margin-bottom: 30px;
                font-size: 14px;
            }
            .upload-area {
                border: 2px dashed #ccc;
                border-radius: 8px;
                padding: 40px;
                text-align: center;
                background: #fafafa;
                transition: all 0.3s;
                cursor: pointer;
                margin-bottom: 20px;
            }
            .upload-area:hover {
                border-color: #007bff;
                background: #f0f7ff;
            }
            .upload-area.dragover {
                border-color: #007bff;
                background: #e7f3ff;
            }
            .upload-area input[type="file"] {
                display: none;
            }
            .upload-icon {
                font-size: 48px;
                margin-bottom: 10px;
            }
            .file-info {
                margin: 20px 0;
                padding: 15px;
                background: #f8f9fa;
                border-radius: 5px;
                display: none;
            }
            .file-info.active {
                display: block;
            }
            .file-info-item {
                margin: 5px 0;
                color: #555;
            }
            .controls { 
                margin: 20px 0; 
            }
            button { 
                padding: 12px 24px; 
                margin: 5px; 
                background: #007bff; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer;
                font-size: 16px;
                transition: background 0.3s;
            }
            button:hover:not(:disabled) { 
                background: #0056b3; 
            }
            button:disabled { 
                background: #ccc; 
                cursor: not-allowed; 
            }
            .status { 
                margin: 15px 0; 
                padding: 15px; 
                border-radius: 5px;
                display: none;
            }
            .status.active {
                display: block;
            }
            .status.processing { 
                background: #fff3cd; 
                border: 1px solid #ffeaa7; 
                color: #856404;
            }
            .status.completed { 
                background: #d4edda; 
                border: 1px solid #c3e6cb; 
                color: #155724;
            }
            .status.failed { 
                background: #f8d7da; 
                border: 1px solid #f5c6cb; 
                color: #721c24;
            }
            .result { 
                margin: 20px 0; 
                padding: 20px; 
                background: #f8f9fa; 
                border-radius: 5px;
                display: none;
            }
            .result.active {
                display: block;
            }
            .result h3 {
                margin-top: 0;
                color: #333;
            }
            .result-item {
                margin: 10px 0;
                padding: 8px;
                background: white;
                border-radius: 4px;
            }
            .result-label {
                font-weight: bold;
                color: #555;
            }
            .progress { 
                margin: 15px 0; 
                height: 20px; 
                background: #eee; 
                border-radius: 10px; 
                overflow: hidden; 
                border: 1px solid #ddd;
            }
            .progress-bar { 
                height: 100%; 
                width: 0%; 
                background: linear-gradient(90deg, #28a745, #17a2b8); 
                transition: width .3s ease;
                display: flex;
                align-items: center;
                justify-content: center;
                color: white;
                font-size: 12px;
                font-weight: bold;
            }
            .progress-info {
                margin: 10px 0;
                color: #666;
                font-size: 14px;
            }
            .muted { 
                color: #666; 
                font-size: 12px; 
            }
            .upload-progress {
                margin-top: 15px;
            }
            .download-link {
                display: inline-block;
                margin-top: 15px;
                padding: 10px 20px;
                background: #28a745;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                transition: background 0.3s;
            }
            .download-link:hover {
                background: #218838;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📹 Загрузка видео для обработки</h1>
            <div class="subtitle">Выберите видео файл для анализа. Поддерживается загрузка больших файлов по частям.</div>
            
            <div class="upload-area" id="uploadArea">
                <div class="upload-icon">📁</div>
                <div style="font-size: 18px; margin-bottom: 10px;">Перетащите файл сюда или нажмите для выбора</div>
                <div class="muted">Поддерживаются форматы: MP4, AVI, MOV (максимум 500MB)</div>
                <input type="file" id="fileInput" accept="video/*">
            </div>
            
            <div class="file-info" id="fileInfo">
                <div class="file-info-item"><strong>Файл:</strong> <span id="fileName"></span></div>
                <div class="file-info-item"><strong>Размер:</strong> <span id="fileSize"></span></div>
                <div class="file-info-item"><strong>Частей:</strong> <span id="fileChunks"></span></div>
            </div>
            
            <div class="controls">
                <button id="uploadBtn" onclick="startUpload()" disabled>Начать загрузку</button>
                <button id="cancelBtn" onclick="cancelUpload()" disabled>Отменить</button>
                <button id="resetBtn" onclick="resetUpload()" style="display: none;">Выбрать другой файл</button>
            </div>
            
            <div id="status" class="status"></div>
            
            <div class="upload-progress" id="uploadProgress" style="display: none;">
                <div class="progress-info" id="uploadProgressInfo">Загрузка файла...</div>
                <div class="progress">
                    <div id="uploadProgressBar" class="progress-bar">0%</div>
                </div>
            </div>
            
            <div id="progressWrap" style="display: none;">
                <div class="progress-info" id="jobInfo"></div>
                <div class="progress">
                    <div id="progressBar" class="progress-bar">0%</div>
                </div>
                <div class="muted" id="progressText">Ожидание начала обработки...</div>
            </div>
            
            <div id="result" class="result"></div>
        </div>

        <script>
            const CHUNK_SIZE = 10 * 1024 * 1024; // 10MB чанки
            let selectedFile = null;
            let uploadId = null;
            let jobId = null;
            let totalChunks = 0;
            let uploadedChunks = 0;
            let pollTimer = null;
            let isUploading = false;

            // Инициализация
            const uploadArea = document.getElementById('uploadArea');
            const fileInput = document.getElementById('fileInput');
            const fileInfo = document.getElementById('fileInfo');
            const uploadBtn = document.getElementById('uploadBtn');
            const cancelBtn = document.getElementById('cancelBtn');

            // Обработка клика по области загрузки
            uploadArea.addEventListener('click', () => {
                fileInput.click();
            });

            // Обработка выбора файла
            fileInput.addEventListener('change', (e) => {
                handleFileSelect(e.target.files[0]);
            });

            // Drag and drop
            uploadArea.addEventListener('dragover', (e) => {
                e.preventDefault();
                uploadArea.classList.add('dragover');
            });

            uploadArea.addEventListener('dragleave', () => {
                uploadArea.classList.remove('dragover');
            });

            uploadArea.addEventListener('drop', (e) => {
                e.preventDefault();
                uploadArea.classList.remove('dragover');
                if (e.dataTransfer.files.length > 0) {
                    handleFileSelect(e.dataTransfer.files[0]);
                }
            });

            function handleFileSelect(file) {
                if (!file) return;
                
                // Проверка типа файла
                if (!file.type.startsWith('video/')) {
                    showStatus('Пожалуйста, выберите видео файл', 'failed');
                    return;
                }
                
                selectedFile = file;
                const fileSizeMB = (file.size / 1024 / 1024).toFixed(2);
                totalChunks = Math.ceil(file.size / CHUNK_SIZE);
                
                // Показываем информацию о файле
                document.getElementById('fileName').textContent = file.name;
                document.getElementById('fileSize').textContent = fileSizeMB + ' MB';
                document.getElementById('fileChunks').textContent = totalChunks;
                fileInfo.classList.add('active');
                
                uploadBtn.disabled = false;
                showStatus('Файл выбран. Нажмите "Начать загрузку"', 'processing');
            }

            async function startUpload() {
                if (!selectedFile || isUploading) return;
                
                isUploading = true;
                uploadBtn.disabled = true;
                cancelBtn.disabled = false;
                document.getElementById('uploadProgress').style.display = 'block';
                showStatus('Инициализация загрузки...', 'processing');
                
                try {
                    // 1. Инициализация
                    const initResponse = await fetch('/api/v1/upload/init', {
                        method: 'POST',
                        body: (() => {
                            const formData = new FormData();
                            formData.append('filename', selectedFile.name);
                            formData.append('total_size', selectedFile.size.toString());
                            formData.append('total_chunks', totalChunks.toString());
                            return formData;
                        })()
                    });
                    
                    if (!initResponse.ok) {
                        const error = await initResponse.json();
                        throw new Error(error.detail || 'Ошибка инициализации');
                    }
                    
                    const initData = await initResponse.json();
                    uploadId = initData.upload_id;
                    console.log('✅ Инициализация успешна. Upload ID:', uploadId);
                    showStatus('Загрузка файла по частям...', 'processing');
                    
                    // 2. Загрузка чанков
                    uploadedChunks = 0;
                    for (let i = 0; i < totalChunks; i++) {
                        if (!isUploading) {
                            throw new Error('Загрузка отменена');
                        }
                        
                        const start = i * CHUNK_SIZE;
                        const end = Math.min(start + CHUNK_SIZE, selectedFile.size);
                        const chunk = selectedFile.slice(start, end);
                        
                        const chunkFormData = new FormData();
                        chunkFormData.append('upload_id', uploadId);
                        chunkFormData.append('chunk_number', (i + 1).toString());
                        chunkFormData.append('chunk', chunk, `chunk_${i}`);
                        
                        const chunkResponse = await fetch('/api/v1/upload/chunk', {
                            method: 'POST',
                            body: chunkFormData
                        });
                        
                        if (!chunkResponse.ok) {
                            const error = await chunkResponse.json();
                            throw new Error(error.detail || `Ошибка загрузки части ${i + 1}`);
                        }
                        
                        uploadedChunks++;
                        const uploadProgress = ((uploadedChunks / totalChunks) * 100).toFixed(1);
                        document.getElementById('uploadProgressBar').style.width = uploadProgress + '%';
                        document.getElementById('uploadProgressBar').textContent = uploadProgress + '%';
                        document.getElementById('uploadProgressInfo').textContent = 
                            `Загружено ${uploadedChunks}/${totalChunks} частей (${uploadProgress}%)`;
                        
                        console.log(`📤 Чанк ${uploadedChunks}/${totalChunks} загружен (${uploadProgress}%)`);
                    }
                    
                    // 3. Завершение загрузки
                    showStatus('Завершение загрузки и запуск обработки...', 'processing');
                    
                    const completeFormData = new FormData();
                    completeFormData.append('upload_id', uploadId);
                    
                    const completeResponse = await fetch('/api/v1/upload/complete', {
                        method: 'POST',
                        body: completeFormData
                    });
                    
                    if (!completeResponse.ok) {
                        const error = await completeResponse.json();
                        throw new Error(error.detail || 'Ошибка завершения загрузки');
                    }
                    
                    const completeData = await completeResponse.json();
                    jobId = completeData.job_id;
                    
                    console.log('✅ Загрузка завершена. Job ID:', jobId);
                    showStatus('Файл загружен успешно. Обработка начата...', 'completed');
                    document.getElementById('uploadProgress').style.display = 'none';
                    
                    // Сбрасываем флаг загрузки, но оставляем кнопки отключенными до завершения обработки
                    isUploading = false;
                    uploadBtn.disabled = true;
                    cancelBtn.disabled = true;
                    
                    // 4. Отслеживание обработки
                    showProgress(0, jobId);
                    startJobPolling(jobId);
                    
                } catch (error) {
                    console.error('❌ Ошибка загрузки:', error);
                    showStatus('Ошибка: ' + error.message, 'failed');
                    document.getElementById('uploadProgress').style.display = 'none';
                    isUploading = false;
                    uploadBtn.disabled = false;
                    cancelBtn.disabled = true;
                }
            }

            function cancelUpload() {
                if (confirm('Вы уверены, что хотите отменить загрузку?')) {
                    isUploading = false;
                    uploadBtn.disabled = false;
                    cancelBtn.disabled = true;
                    document.getElementById('uploadProgress').style.display = 'none';
                    document.getElementById('progressWrap').style.display = 'none';
                    if (pollTimer) clearInterval(pollTimer);
                    showStatus('Загрузка отменена', 'failed');
                    
                    if (uploadId) {
                        // Отменяем загрузку на сервере
                        fetch(`/api/v1/upload/${uploadId}`, {
                            method: 'DELETE'
                        }).catch(err => console.error('Ошибка отмены загрузки:', err));
                    }
                }
            }

            function resetUpload() {
                // Сбрасываем все переменные
                selectedFile = null;
                uploadId = null;
                jobId = null;
                totalChunks = 0;
                uploadedChunks = 0;
                isUploading = false;
                
                // Очищаем интервалы
                if (pollTimer) {
                    clearInterval(pollTimer);
                    pollTimer = null;
                }
                
                // Сбрасываем UI
                fileInput.value = '';
                fileInfo.classList.remove('active');
                document.getElementById('uploadProgress').style.display = 'none';
                document.getElementById('progressWrap').style.display = 'none';
                document.getElementById('result').classList.remove('active');
                document.getElementById('status').classList.remove('active');
                document.getElementById('resetBtn').style.display = 'none';
                
                // Сбрасываем кнопки
                uploadBtn.disabled = true;
                cancelBtn.disabled = true;
            }

            function showStatus(message, status) {
                const statusDiv = document.getElementById('status');
                statusDiv.textContent = message;
                statusDiv.className = `status ${status} active`;
            }

            function showProgress(percent, id) {
                const wrap = document.getElementById('progressWrap');
                const bar = document.getElementById('progressBar');
                const text = document.getElementById('progressText');
                const info = document.getElementById('jobInfo');
                wrap.style.display = 'block';
                const progressValue = Math.max(0, Math.min(100, percent));
                bar.style.width = progressValue + '%';
                bar.textContent = progressValue + '%';
                text.textContent = id ? `Обработка задачи ${id}...` : 'Ожидание...';
                info.textContent = id ? `Job ID: ${id}` : '';
            }

            function showResult(result) {
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = `
                    <h3>✅ Результат обработки:</h3>
                    <div class="result-item">
                        <span class="result-label">Повторения:</span> ${result.reps || 'N/A'}
                    </div>
                    <div class="result-item">
                        <span class="result-label">Скорость:</span> ${result.velocity || 'N/A'}
                    </div>
                    <div class="result-item">
                        <span class="result-label">Точность:</span> ${result.bar_path_accuracy_percent || 'N/A'}%
                    </div>
                    <div class="result-item">
                        <span class="result-label">Путь штанги:</span> ${result.bar_path || 'N/A'}
                    </div>
                    <div class="result-item">
                        <span class="result-label">Усталость:</span> ${result.fatigue || 'N/A'}
                    </div>
                    <div class="result-item">
                        <span class="result-label">Время под напряжением:</span> ${result.tut || 'N/A'} сек
                    </div>
                    ${jobId ? `<a href="/api/v1/download/${jobId}" class="download-link">📥 Скачать обработанное видео</a>` : ''}
                `;
                resultDiv.classList.add('active');
                showProgress(100, jobId);
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
                            showStatus('Обработка завершена успешно!', 'completed');
                            
                            // Разрешаем загрузку нового файла
                            uploadBtn.disabled = false;
                            cancelBtn.disabled = true;
                            document.getElementById('resetBtn').style.display = 'inline-block';
                            
                            try {
                                const rr = await fetch(`/api/v1/result/${id}`);
                                if (rr.ok) {
                                    const data = await rr.json();
                                    if (data && data.result) {
                                        showResult(data.result);
                                    }
                                }
                            } catch (e) {
                                console.error('Ошибка получения результата:', e);
                            }
                        } else if (job.status === 'failed') {
                            clearInterval(pollTimer);
                            showStatus('Ошибка обработки: ' + (job.error_message || 'Неизвестная ошибка'), 'failed');
                            
                            // Разрешаем загрузку нового файла даже при ошибке
                            uploadBtn.disabled = false;
                            cancelBtn.disabled = true;
                            document.getElementById('resetBtn').style.display = 'inline-block';
                        }
                    } catch (e) {
                        console.error('Ошибка запроса статуса job:', e);
                    }
                }, 2000);
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


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
            "video_upload": {
                "page": "/video-upload",
                "description": "Веб-интерфейс для загрузки видео по частям"
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
