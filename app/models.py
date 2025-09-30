"""
Модели данных для NeiroFitnessApp
"""
from typing import Dict, Optional
from datetime import datetime
from pydantic import BaseModel


class JobStatus(BaseModel):
    """Модель статуса задачи обработки видео"""
    job_id: str
    status: str  # pending, processing, completed, failed
    progress: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    result: Optional[Dict] = None
    output_file: Optional[str] = None


class ChunkUploadResponse(BaseModel):
    """Модель ответа загрузки части файла"""
    upload_id: str
    chunk_number: int
    total_chunks: int
    uploaded_size: int
    message: str


class UploadSession(BaseModel):
    """Модель сессии загрузки файла"""
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
    """Модель сессии WebRTC стрима"""
    session_id: str
    client_id: str
    status: str = "active"  # active, processing, completed, failed
    created_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[Dict] = None
    output_file: Optional[str] = None
    error_message: Optional[str] = None
    job_id: Optional[str] = None


class ProcessingResult(BaseModel):
    """Модель результата обработки"""
    session_id: str
    status: str
    result: Optional[Dict] = None
    error_message: Optional[str] = None
