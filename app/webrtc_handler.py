"""
WebRTC обработчик для NeiroFitnessApp
"""
import queue
import logging
from typing import Dict
import numpy as np
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack

from app.config import Config

logger = logging.getLogger(__name__)


class ProcessedVideoTrack(VideoStreamTrack):
    """Кастомный VideoStreamTrack для обработки кадров"""
    
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
    """Обработчик WebRTC соединений"""
    
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
