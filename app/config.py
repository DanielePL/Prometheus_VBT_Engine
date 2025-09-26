"""
Конфигурация приложения NeiroFitnessApp
"""
import os


class Config:
    """Основные настройки приложения"""
    
    # Настройки файлов
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
    CHUNK_SIZE = 8192  # Размер чанка для чтения
    
    # Настройки обработки
    MAX_CONCURRENT_JOBS = 3  # Максимум одновременных задач
    
    # Директории
    TEMP_DIR = "/tmp/neirofitness"
    OUTPUT_DIR = "/tmp/neirofitness/output"
    
    # Настройки видео
    FRAME_RATE = 30
    FRAME_WIDTH = 640
    FRAME_HEIGHT = 480
    
    # Настройки сервера
    HOST = "0.0.0.0"
    PORT = 8000
    
    @classmethod
    def create_directories(cls):
        """Создание необходимых директорий"""
        os.makedirs(cls.TEMP_DIR, exist_ok=True)
        os.makedirs(cls.OUTPUT_DIR, exist_ok=True)
    
    @classmethod
    def get_max_file_size_mb(cls) -> float:
        """Получить максимальный размер файла в MB"""
        return cls.MAX_FILE_SIZE / (1024 * 1024)
