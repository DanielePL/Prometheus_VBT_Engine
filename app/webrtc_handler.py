"""
WebRTC обработчик для NeiroFitnessApp
"""
import asyncio
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
        self.recorder_started: Dict[str, bool] = {}  # Отслеживаем, запущен ли recorder
        self.peer_connections: Dict[str, RTCPeerConnection] = {}
    
    def is_recording(self, session_id: str) -> bool:
        """Проверить, ведётся ли запись для данной сессии"""
        return self.recorder_started.get(session_id, False)
    
    async def handle_offer(self, offer: str, session_id: str) -> str:
        """Обработка WebRTC offer"""
        logger.info(f"🔵 Получен WebRTC offer от клиента для сессии {session_id}")
        
        # Проверяем SDP
        has_video = "m=video" in offer
        has_audio = "m=audio" in offer
        logger.info(f"SDP содержит: видео={has_video}, аудио={has_audio}")
        
        if not has_video:
            logger.error(f"❌ SDP не содержит видео трек! Сессия {session_id}")
        
        pc = RTCPeerConnection()
        self.pcs.add(pc)
        self.peer_connections[session_id] = pc
        
        logger.info(f"RTCPeerConnection создан для сессии {session_id}")

        # Инициализируем рекордер для записи входящего видео потока
        input_video_path = os.path.join(Config.TEMP_DIR, f"input_{session_id}.mp4")
        recorder = MediaRecorder(input_video_path)
        self.recorders[session_id] = recorder
        self.recorder_started[session_id] = False  # Изначально не запущен
        
        logger.info(f"MediaRecorder создан для сессии {session_id} → {input_video_path}")

        @pc.on("track")
        async def on_track(track):
            try:
                logger.info(f"Получен трек от клиента: тип={track.kind}, id={track.id}, сессия={session_id}")
                
                if track.kind == "video":
                    # Добавляем входящий трек в рекордер и запускаем запись
                    recorder.addTrack(track)
                    logger.info(f"Видео трек добавлен в recorder для сессии {session_id}")
                    
                    # Стартуем запись, если еще не запущена
                    if not self.recorder_started.get(session_id, False):
                        await recorder.start()
                        self.recorder_started[session_id] = True
                        logger.info(f"✅ Запись видео ЗАПУЩЕНА для сессии {session_id} → {input_video_path}")
                    else:
                        logger.warning(f"Recorder уже запущен для сессии {session_id}")
                else:
                    logger.info(f"Получен не-видео трек (тип: {track.kind}) для сессии {session_id}, игнорируем")
            except Exception as e:
                logger.error(f"❌ Ошибка обработчика track для сессии {session_id}: {e}", exc_info=True)
        # Обрабатываем offer
        logger.info(f"Установка remote description для сессии {session_id}")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        logger.info(f"Remote description установлен для сессии {session_id}")
        
        # Создаем answer
        logger.info(f"Создание answer для сессии {session_id}")
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        
        logger.info(f"✅ Answer создан и установлен для сессии {session_id}")
        logger.info(f"Answer SDP содержит видео: {'m=video' in pc.localDescription.sdp}")
        
        return pc.localDescription.sdp
    
    async def close_connection(self, session_id: str):
        """Закрытие соединения"""
        # Останавливаем запись только если она была запущена
        recorder = self.recorders.pop(session_id, None)
        was_started = self.recorder_started.pop(session_id, False)
        
        if recorder is not None and was_started:
            try:
                await recorder.stop()
                logger.info(f"Остановлена запись видео для сессии {session_id}")
                
                # ВАЖНО: MediaRecorder нужно время на финализацию записи
                # Ждём 1 секунду, чтобы файл был полностью записан на диск
                await asyncio.sleep(1.0)
                logger.info(f"Запись финализирована для сессии {session_id}")
                
            except Exception as e:
                logger.error(f"Ошибка остановки рекордера для {session_id}: {e}")
        elif recorder is not None and not was_started:
            logger.warning(f"Рекордер для сессии {session_id} не был запущен (видео трек не получен)")

        # Закрываем конкретное соединение
        pc = self.peer_connections.pop(session_id, None)
        if pc is not None:
            try:
                await pc.close()
                # Удаляем ссылку на PeerConnection из набора
                self.pcs.discard(pc)
            except Exception as e:
                logger.error(f"Ошибка закрытия RTCPeerConnection для {session_id}: {e}")
