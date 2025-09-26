"""
NeiroFitnessApp - Главный файл приложения
Объединяет загрузку файлов, WebRTC стриминг и обработку видео
"""
import asyncio
import logging
import concurrent.futures
from typing import Dict

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from app.config import Config
from app.models import JobStatus, UploadSession, StreamSession
from app.routes_simple import job_router, upload_router, webrtc_router, main_router
from app.webrtc_handler import WebRTCHandler

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание приложения
app = FastAPI(
    title="NeiroFitnessApp",
    version="1.0.0",
    description="Unified Video Processing API with Chunked Upload and WebRTC Streaming"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Создание директорий
Config.create_directories()

# Глобальное хранилище данных
jobs: Dict[str, JobStatus] = {}
upload_sessions: Dict[str, UploadSession] = {}
stream_sessions: Dict[str, StreamSession] = {}
active_connections: Dict[str, any] = {}
active_jobs = [0]  # Используем список для передачи по ссылке

# Семафор для ограничения одновременных задач
job_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_JOBS)

# Пул потоков для CPU-интенсивных задач
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_JOBS)

# WebRTC обработчик
webrtc_handler = WebRTCHandler()


# Dependency injection для маршрутов
def get_jobs() -> Dict[str, JobStatus]:
    return jobs


def get_upload_sessions() -> Dict[str, UploadSession]:
    return upload_sessions


def get_stream_sessions() -> Dict[str, StreamSession]:
    return stream_sessions


def get_active_connections() -> Dict[str, any]:
    return active_connections


def get_active_jobs():
    return active_jobs


def get_job_semaphore():
    return job_semaphore


def get_thread_pool():
    return thread_pool


def get_webrtc_handler() -> WebRTCHandler:
    return webrtc_handler


# Глобальные переменные для доступа из роутеров
app.state.jobs = jobs
app.state.upload_sessions = upload_sessions
app.state.stream_sessions = stream_sessions
app.state.active_connections = active_connections
app.state.active_jobs = active_jobs
app.state.job_semaphore = job_semaphore
app.state.thread_pool = thread_pool
app.state.webrtc_handler = webrtc_handler

# Подключение роутеров
app.include_router(main_router)
app.include_router(job_router)
app.include_router(upload_router)
app.include_router(webrtc_router)


@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске"""
    logger.info("Запуск NeiroFitnessApp...")
    logger.info(f"Максимум одновременных задач: {Config.MAX_CONCURRENT_JOBS}")
    logger.info(f"Максимальный размер файла: {Config.get_max_file_size_mb():.1f}MB")
    logger.info(f"Временная директория: {Config.TEMP_DIR}")
    logger.info(f"Выходная директория: {Config.OUTPUT_DIR}")


@app.on_event("shutdown")
async def shutdown_event():
    """Корректное завершение работы"""
    logger.info("Завершение работы приложения...")
    thread_pool.shutdown(wait=True)
    logger.info("Пул потоков завершен")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main_new:app",
        host=Config.HOST,
        port=Config.PORT,
        reload=True,
        log_level="info"
    )
