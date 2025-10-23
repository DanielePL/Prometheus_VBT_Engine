"""
Утилиты и вспомогательные функции для NeiroFitnessApp
"""
import asyncio
import os
import shutil
import logging
import json
from typing import List, Dict
from datetime import datetime

from app.config import Config
from app.models import UploadSession, JobStatus, StreamSession
from app.video_process import process_video

logger = logging.getLogger(__name__)


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
        # Гарантируем наличие временной директории
        os.makedirs(Config.TEMP_DIR, exist_ok=True)
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


async def cleanup_upload_session(upload_id: str, upload_sessions: Dict[str, UploadSession]):
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


def process_video_sync(job_id: str, input_path: str, output_path: str, jobs: Dict[str, JobStatus], active_jobs_ref):
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
        os.makedirs(os.path.dirname(final_output) or Config.OUTPUT_DIR, exist_ok=True)
        if not os.path.exists(processed_path):
            raise FileNotFoundError(f"Результирующий файл не найден: {processed_path}")
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
        active_jobs_ref[0] -= 1


async def process_single_video(job_id: str, input_path: str, output_path: str, jobs: Dict[str, JobStatus], active_jobs_ref, thread_pool):
    """Асинхронная обертка для обработки видео"""
    try:
        # Запускаем обработку в отдельном потоке
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            thread_pool,
            process_video_sync,
            job_id,
            input_path,
            output_path,
            jobs,
            active_jobs_ref
        )
    except Exception as e:
        logger.error(f"Ошибка в асинхронной обертке для {job_id}: {e}")
        jobs[job_id].status = "failed"
        jobs[job_id].error_message = str(e)
        jobs[job_id].completed_at = datetime.now()
        active_jobs_ref[0] -= 1


def process_video_stream_sync(session_id: str, video_path: str, stream_sessions: Dict[str, StreamSession]):
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
        os.makedirs(os.path.dirname(final_output) or Config.OUTPUT_DIR, exist_ok=True)
        if not os.path.exists(processed_path):
            raise FileNotFoundError(f"Результирующий файл не найден: {processed_path}")
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


async def process_video_stream(session_id: str, stream_sessions: Dict[str, StreamSession], active_connections: Dict):
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
            temp_video_path,
            stream_sessions
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
