import asyncio
import os
import shutil
import uuid
import json
import logging
import mimetypes
import concurrent.futures
import threading
import queue
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.signaling import TcpSocketSignaling
import av

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, HTMLResponse, JSONResponse, StreamingResponse
from starlette.responses import Response
from pydantic import BaseModel, Field

from app.video_process import process_video

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="NeiroFitnessApp", version="1.0.0")

@app.on_event("shutdown")
async def shutdown_event():
    """Корректное завершение работы"""
    logger.info("Завершение работы приложения...")
    thread_pool.shutdown(wait=True)
    logger.info("Пул потоков завершен")

# Конфигурация
class Config:
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
    CHUNK_SIZE = 8192  # Размер чанка для чтения
    MAX_CONCURRENT_JOBS = 3  # Максимум одновременных задач
    TEMP_DIR = "/tmp/neirofitness"
    OUTPUT_DIR = "/tmp/neirofitness/output"
    FRAME_RATE = 30
    FRAME_WIDTH = 640
    FRAME_HEIGHT = 480

# Создаем директории если не существуют
os.makedirs(Config.TEMP_DIR, exist_ok=True)
os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

# Модели данных
class JobStatus(BaseModel):
    job_id: str
    status: str  # pending, processing, completed, failed
    progress: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    result: Optional[Dict] = None
    output_file: Optional[str] = None

class ChunkUploadResponse(BaseModel):
    upload_id: str
    chunk_number: int
    total_chunks: int
    uploaded_size: int
    message: str

class UploadSession(BaseModel):
    upload_id: str
    filename: str
    total_size: int
    total_chunks: int
    uploaded_chunks: int = 0
    uploaded_size: int = 0
    chunks: Dict[int, str] = {}  # chunk_number -> file_path
    created_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "uploading"  # uploading, completed, failed

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

# Глобальное хранилище задач
jobs: Dict[str, JobStatus] = {}
active_jobs = 0

# Хранилище сессий загрузки
upload_sessions: Dict[str, UploadSession] = {}

# Глобальное хранилище сессий WebRTC
stream_sessions: Dict[str, StreamSession] = {}
active_connections: Dict[str, WebSocket] = {}

# Семафор для ограничения одновременных задач
job_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_JOBS)

# Пул потоков для CPU-интенсивных задач
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_JOBS)

# Утилиты
async def cleanup_temp_files(file_paths: List[str]):
    """Очистка временных файлов"""
    for file_path in file_paths:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Удален временный файл: {file_path}")
        except Exception as e:
            logger.error(f"Ошибка при удалении файла {file_path}: {e}")

async def assemble_file_from_chunks(upload_session: UploadSession) -> str:
    """Сборка файла из частей"""
    try:
        # Создаем финальный файл
        final_file_path = os.path.join(Config.TEMP_DIR, f"assembled_{upload_session.upload_id}.mp4")
        
        with open(final_file_path, "wb") as final_file:
            total_written = 0
            # Собираем части в правильном порядке
            for chunk_num in range(1, upload_session.total_chunks + 1):
                if chunk_num not in upload_session.chunks:
                    raise ValueError(f"Отсутствует часть {chunk_num}")
                
                chunk_path = upload_session.chunks[chunk_num]
                if not os.path.exists(chunk_path):
                    raise ValueError(f"Файл части {chunk_num} не найден: {chunk_path}")
                
                chunk_size = os.path.getsize(chunk_path)
                logger.info(f"Собираем часть {chunk_num}, размер: {chunk_size} байт")
                
                # Читаем и записываем часть
                with open(chunk_path, "rb") as chunk_file:
                    written = shutil.copyfileobj(chunk_file, final_file)
                    total_written += chunk_size
                    logger.info(f"Записано {chunk_size} байт, общий размер: {total_written} байт")
        
        # Проверяем размер собранного файла
        final_size = os.path.getsize(final_file_path)
        if final_size != upload_session.total_size:
            os.remove(final_file_path)
            raise ValueError(f"Размер собранного файла не совпадает: {final_size} != {upload_session.total_size}")
        
        logger.info(f"Файл собран успешно: {final_file_path}, размер: {final_size / (1024*1024):.1f}MB")
        return final_file_path
        
    except Exception as e:
        logger.error(f"Ошибка при сборке файла: {e}")
        raise

async def cleanup_upload_session(upload_id: str):
    """Очистка сессии загрузки и связанных файлов"""
    if upload_id not in upload_sessions:
        return
    
    session = upload_sessions[upload_id]
    files_to_cleanup = list(session.chunks.values())
    
    # Добавляем собранный файл если есть
    assembled_file = os.path.join(Config.TEMP_DIR, f"assembled_{upload_id}.mp4")
    if os.path.exists(assembled_file):
        files_to_cleanup.append(assembled_file)
    
    await cleanup_temp_files(files_to_cleanup)
    
    # Удаляем сессию
    if upload_id in upload_sessions:
        del upload_sessions[upload_id]

def process_video_sync(job_id: str, input_path: str, output_path: str):
    """Синхронная обработка видео в отдельном потоке"""
    try:
        # Обновляем статус
        jobs[job_id].status = "processing"
        jobs[job_id].progress = 10
        
        # Проверяем размер файла
        file_size = os.path.getsize(input_path)
        if file_size > Config.MAX_FILE_SIZE:
            raise ValueError(f"Файл слишком большой: {file_size / (1024*1024):.1f}MB (максимум {Config.MAX_FILE_SIZE / (1024*1024):.1f}MB)")
        
        logger.info(f"Начинаем обработку видео {job_id}, размер: {file_size / (1024*1024):.1f}MB")
        
        # Обрабатываем видео
        jobs[job_id].progress = 30
        result, processed_path = process_video(input_path, output_path)
        
        jobs[job_id].progress = 90
        
        # Перемещаем результат в выходную директорию
        final_output = os.path.join(Config.OUTPUT_DIR, f"{job_id}_processed.mp4")
        shutil.move(processed_path, final_output)
        
        # Обновляем статус
        jobs[job_id].status = "completed"
        jobs[job_id].progress = 100
        jobs[job_id].completed_at = datetime.now()
        jobs[job_id].result = result
        jobs[job_id].output_file = final_output
        
        logger.info(f"Обработка завершена для {job_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке {job_id}: {e}")
        jobs[job_id].status = "failed"
        jobs[job_id].error_message = str(e)
        jobs[job_id].completed_at = datetime.now()
    finally:
        global active_jobs
        active_jobs -= 1

async def process_single_video(job_id: str, input_path: str, output_path: str):
    """Асинхронная обертка для обработки видео"""
    global active_jobs
    
    try:
        # Запускаем обработку в отдельном потоке
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            thread_pool,
            process_video_sync,
            job_id,
            input_path,
            output_path
        )
    except Exception as e:
        logger.error(f"Ошибка в асинхронной обертке для {job_id}: {e}")
        jobs[job_id].status = "failed"
        jobs[job_id].error_message = str(e)
        jobs[job_id].completed_at = datetime.now()
        active_jobs -= 1

# WebRTC классы и функции
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

# API маршруты
@app.get("/api/v1/job/{job_id}")
async def get_job_status(job_id: str):
    """Получить статус задачи"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return jobs[job_id]

@app.get("/api/v1/jobs")
async def get_all_jobs():
    """Получить все задачи"""
    return {"jobs": list(jobs.values())}

@app.get("/api/v1/result/{job_id}")
async def get_job_result(job_id: str):
    """Получить результат обработки"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Обработка еще не завершена")
    
    return {"job_id": job_id, "result": job.result}

@app.get("/api/v1/download/{job_id}")
async def download_processed_video(job_id: str):
    """Скачать обработанное видео"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    job = jobs[job_id]
    if job.status != "completed" or not job.output_file:
        raise HTTPException(status_code=400, detail="Видео еще не готово")
    
    if not os.path.exists(job.output_file):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    from fastapi.responses import FileResponse
    return FileResponse(
        job.output_file,
        media_type="video/mp4",
        filename=f"processed_{job_id}.mp4"
    )

@app.delete("/api/v1/job/{job_id}")
async def delete_job(job_id: str):
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

# Upload маршруты
@app.post("/api/v1/upload/init")
async def init_chunked_upload(
    filename: str = Form(...),
    total_size: int = Form(...),
    total_chunks: int = Form(...)
):
    """Инициализация chunked upload"""
    # Валидация параметров
    if total_size > Config.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Файл слишком большой: {total_size / (1024*1024):.1f}MB (максимум {Config.MAX_FILE_SIZE / (1024*1024):.1f}MB)"
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

@app.post("/api/v1/upload/chunk", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_number: int = Form(...),
    chunk: UploadFile = File(...)
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

@app.post("/api/v1/upload/complete")
async def complete_chunked_upload(
    background_tasks: BackgroundTasks,
    upload_id: str = Form(...)
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
            global active_jobs
            active_jobs += 1
            background_tasks.add_task(
                process_single_video,
                job_id,
                assembled_file,
                temp_output
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
            upload_id
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

@app.get("/api/v1/upload/{upload_id}")
async def get_upload_status(upload_id: str):
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

@app.delete("/api/v1/upload/{upload_id}")
async def cancel_upload(upload_id: str):
    """Отменить загрузку"""
    if upload_id not in upload_sessions:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена")
    
    await cleanup_upload_session(upload_id)
    
    return {"message": "Загрузка отменена"}

@app.get("/api/v1/stats")
async def get_system_stats():
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
        "max_file_size_mb": Config.MAX_FILE_SIZE / (1024 * 1024)
    }


@app.get("/")
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

# WebRTC маршруты
@app.get("/webrtc")
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