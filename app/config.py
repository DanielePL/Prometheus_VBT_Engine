"""
Конфигурация приложения NeiroFitnessApp

Unterstuetzt Umgebungsvariablen fuer Render.com Deployment:
- MAX_FILE_SIZE_MB: Maximale Dateigroesse in MB (default: 500)
- MAX_CONCURRENT_JOBS: Maximale gleichzeitige Verarbeitungen (default: 2)
- PORT: Server-Port (default: 8000)
"""
import os


class Config:
    """Основные настройки приложения"""

    # Настройки файлов (from environment or default)
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "500")) * 1024 * 1024
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "8192"))

    # Настройки обработки (reduced for stability on cloud platforms)
    MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

    # Директории (relativ zum Projektordner)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    TEMP_DIR = os.getenv("TEMP_DIR", os.path.join(BASE_DIR, "uploads", "neirofitness"))
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", os.path.join(BASE_DIR, "uploads", "neirofitness", "output"))

    # Настройки видео
    FRAME_RATE = 30
    FRAME_WIDTH = 640
    FRAME_HEIGHT = 480

    # Настройки сервера
    HOST = "0.0.0.0"
    PORT = int(os.getenv("PORT", "8000"))

    @classmethod
    def create_directories(cls):
        """Создание необходимых директорий"""
        os.makedirs(cls.TEMP_DIR, exist_ok=True)
        os.makedirs(cls.OUTPUT_DIR, exist_ok=True)

    @classmethod
    def get_max_file_size_mb(cls) -> float:
        """Получить максимальный размер файла в MB"""
        return cls.MAX_FILE_SIZE / (1024 * 1024)
