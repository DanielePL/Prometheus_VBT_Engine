import asyncio
import os
import shutil
import uuid
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path
import concurrent.futures
import threading

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Form
from fastapi.responses import JSONResponse, StreamingResponse
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

# Глобальное хранилище задач
jobs: Dict[str, JobStatus] = {}
active_jobs = 0

# Хранилище сессий загрузки
upload_sessions: Dict[str, UploadSession] = {}

# Семафор для ограничения одновременных задач
job_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_JOBS)

# Пул потоков для CPU-интенсивных задач
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_JOBS)

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

