"""
WebRTC обработчик для NeiroFitnessApp
"""
import logging
import os
from typing import Dict
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRecorder

from app.config import Config

logger = logging.getLogger(__name__)


class WebRTCHandler:
    """Обработчик WebRTC соединений"""
    
    def __init__(self):
        self.pcs = set()
        self.recorders: Dict[str, MediaRecorder] = {}
        self.peer_connections: Dict[str, RTCPeerConnection] = {}
    
    async def handle_offer(self, offer: str, session_id: str) -> str:
        """Обработка WebRTC offer"""
        pc = RTCPeerConnection()
        self.pcs.add(pc)
        self.peer_connections[session_id] = pc

        # Инициализируем рекордер для записи входящего видео потока
        input_video_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
        recorder = MediaRecorder(input_video_path)
        self.recorders[session_id] = recorder

        @pc.on("track")
        async def on_track(track):
            try:
                if track.kind == "video":
                    # Добавляем входящий трек в рекордер и запускаем запись
                    recorder.addTrack(track)
                    # Стартуем запись, если еще не запущена
                    await recorder.start()
                    logger.info(f"Старт записи видео для сессии {session_id} в {input_video_path}")
            except Exception as e:
                logger.error(f"Ошибка обработчика track для сессии {session_id}: {e}")
        # Обрабатываем offer
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        
        # Создаем answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        
        return pc.localDescription.sdp
    
    async def close_connection(self, session_id: str):
        """Закрытие соединения"""
        # Останавливаем запись если велась
        recorder = self.recorders.pop(session_id, None)
        if recorder is not None:
            try:
                await recorder.stop()
                logger.info(f"Остановлена запись видео для сессии {session_id}")
            except Exception as e:
                logger.error(f"Ошибка остановки рекордера для {session_id}: {e}")

        # Закрываем конкретное соединение
        pc = self.peer_connections.pop(session_id, None)
        if pc is not None:
            try:
                await pc.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия RTCPeerConnection для {session_id}: {e}")

        # Удаляем ссылку на PeerConnection из набора
        if session_id in self.peer_connections:
            self.pcs.discard(self.peer_connections[session_id])
