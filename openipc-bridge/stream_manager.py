"""Stream Manager for OpenIPC Bridge - вдохновлен Frigate"""
import subprocess as sp
import threading
import time
import logging
import os
import signal
from typing import Dict, Optional, Callable
from datetime import datetime
import queue

logger = logging.getLogger(__name__)

class StreamManager:
    """Управляет FFmpeg процессами для HLS стриминга с автоматическим перезапуском"""
    
    def __init__(self, camera_ip: str, username: str, password: str, 
                 stream_type: str = 'main', on_status_change: Optional[Callable] = None):
        self.camera_ip = camera_ip
        self.username = username
        self.password = password
        self.stream_type = stream_type
        self.output_name = f"{camera_ip}_{stream_type}"
        self.on_status_change = on_status_change
        
        # Директории и файлы
        self.base_hls_dir = "/tmp/hls"
        self.hls_dir = f"{self.base_hls_dir}/{self.output_name}"
        self.log_file = f"{self.hls_dir}/ffmpeg.log"
        self.playlist_path = f"{self.hls_dir}/playlist.m3u8"
        
        # Параметры потока
        self.stream_path = '/stream=0' if stream_type == 'main' else '/stream=1'
        self.rtsp_url = f"rtsp://{username}:{password}@{camera_ip}:554{self.stream_path}"
        
        # Процесс и потоки
        self.process: Optional[sp.Popen] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._restart_event = threading.Event()
        
        # Статистика и метрики (как во Frigate)
        self.start_time: Optional[float] = None
        self.last_frame_time: float = 0
        self.frames_received: int = 0
        self.restart_count: int = 0
        self.error_count: int = 0
        self.last_error: Optional[str] = None
        
        # Очередь для будущих задач (как во Frigate)
        self.frame_queue: queue.Queue = queue.Queue(maxsize=30)
        
        # Создаем директории
        self._ensure_directories()
        
    def _ensure_directories(self):
        """Создает необходимые директории"""
        try:
            os.makedirs(self.base_hls_dir, exist_ok=True)
            os.makedirs(self.hls_dir, exist_ok=True)
            os.chmod(self.base_hls_dir, 0o777)
            os.chmod(self.hls_dir, 0o777)
            logger.info(f"✅ Created directories: {self.hls_dir}")
        except Exception as e:
            logger.error(f"❌ Failed to create directories: {e}")
        
    def _get_ffmpeg_cmd(self) -> list:
        """Собирает команду FFmpeg для HLS (упрощенная, как во Frigate)"""
        return [
            'ffmpeg',
            '-rtsp_transport', 'tcp',
            '-i', self.rtsp_url,
            '-c:v', 'copy',              # Копируем видео без перекодирования
            '-c:a', 'aac',                # Аудио кодек
            '-f', 'hls',                  # Выходной формат HLS
            '-hls_time', '2',              # Длительность сегмента
            '-hls_list_size', '5',          # Количество сегментов в плейлисте
            '-hls_flags', 'delete_segments', # Удалять старые сегменты
            '-hls_segment_filename', f'{self.hls_dir}/segment_%03d.ts',
            self.playlist_path
        ]
    
    def _stop_ffmpeg(self, force: bool = False):
        """Корректно останавливает FFmpeg процесс (как во Frigate)"""
        if self.process is None:
            return
            
        logger.info(f"Stopping FFmpeg for {self.output_name}...")
        
        if force:
            self.process.kill()
            logger.info(f"Force killed FFmpeg for {self.output_name}")
        else:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
                logger.info(f"FFmpeg for {self.output_name} terminated gracefully")
            except sp.TimeoutExpired:
                logger.warning(f"FFmpeg for {self.output_name} didn't exit, killing...")
                self.process.kill()
                self.process.wait()
                
        self.process = None
        self.last_frame_time = 0
        
    def _read_last_log_lines(self, num_lines: int = 20) -> str:
        """Читает последние строки из лога FFmpeg"""
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r') as f:
                    lines = f.readlines()
                    return ''.join(lines[-num_lines:])
        except Exception as e:
            return f"Error reading log: {e}"
        return "Log file not found"
    
    def _check_playlist_health(self) -> tuple[bool, str]:
        """Проверяет здоровье HLS плейлиста"""
        if not os.path.exists(self.playlist_path):
            return False, "Playlist not found"
            
        try:
            size = os.path.getsize(self.playlist_path)
            if size == 0:
                return False, "Playlist is empty"
                
            # Проверяем, что плейлист обновлялся последние 10 секунд
            mtime = os.path.getmtime(self.playlist_path)
            if time.time() - mtime > 10:
                return False, f"Playlist stale (last update: {datetime.fromtimestamp(mtime).strftime('%H:%M:%S')})"
                
            return True, f"OK (size: {size} bytes)"
        except Exception as e:
            return False, f"Error checking playlist: {e}"
    
    def _monitor_loop(self):
        """Основной цикл мониторинга (как CameraWatchdog во Frigate)"""
        logger.info(f"Monitor started for {self.output_name}")
        
        playlist_check_interval = 2
        process_check_interval = 5
        last_playlist_check = 0
        last_process_check = 0
        consecutive_failures = 0
        
        while not self._stop_event.is_set():
            now = time.time()
            
            # 1. Проверка процесса FFmpeg
            if now - last_process_check >= process_check_interval:
                last_process_check = now
                
                # Если процесс не запущен, запускаем
                if self.process is None:
                    logger.info(f"FFmpeg not running for {self.output_name}, starting...")
                    self._start_ffmpeg()
                    continue
                
                # Проверяем, жив ли процесс
                poll = self.process.poll()
                if poll is not None:
                    self.error_count += 1
                    self.last_error = f"Process died with code {poll}"
                    logger.error(f"FFmpeg for {self.output_name} died: {self.last_error}")
                    
                    # Читаем последние строки лога
                    log_tail = self._read_last_log_lines()
                    logger.error(f"Last logs:\n{log_tail}")
                    
                    # Перезапускаем
                    self.restart_count += 1
                    self._stop_ffmpeg()
                    if self.on_status_change:
                        self.on_status_change(self.output_name, "restarting", self.restart_count)
                    continue
            
            # 2. Проверка HLS плейлиста
            if now - last_playlist_check >= playlist_check_interval:
                last_playlist_check = now
                
                if self.process is not None:
                    healthy, message = self._check_playlist_health()
                    
                    if not healthy:
                        consecutive_failures += 1
                        logger.warning(f"HLS health check failed for {self.output_name}: {message} ({consecutive_failures}/3)")
                        
                        if consecutive_failures >= 3:
                            logger.error(f"Too many HLS failures for {self.output_name}, restarting FFmpeg...")
                            self.restart_count += 1
                            self._stop_ffmpeg()
                            consecutive_failures = 0
                            if self.on_status_change:
                                self.on_status_change(self.output_name, "restarting", self.restart_count)
                    else:
                        consecutive_failures = 0
                        if self.on_status_change and self.restart_count > 0:
                            self.on_status_change(self.output_name, "running", self.restart_count)
            
            # Небольшая пауза, чтобы не нагружать CPU
            self._stop_event.wait(0.5)
        
        logger.info(f"Monitor stopped for {self.output_name}")
    
    def _start_ffmpeg(self):
        """Запускает FFmpeg процесс"""
        try:
            cmd = self._get_ffmpeg_cmd()
            logger.info(f"Starting FFmpeg for {self.output_name}: {' '.join(cmd)}")
            
            # Создаем директорию для лога
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            
            # Запускаем процесс с логированием
            with open(self.log_file, 'w') as f:
                self.process = sp.Popen(
                    cmd,
                    stdout=f,
                    stderr=sp.STDOUT,
                    start_new_session=True  # Важно для корректного завершения
                )
            
            self.start_time = time.time()
            logger.info(f"FFmpeg started for {self.output_name} with PID {self.process.pid}")
            
            if self.on_status_change:
                self.on_status_change(self.output_name, "starting", 0)
                
        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            logger.error(f"Failed to start FFmpeg for {self.output_name}: {e}")
            self.process = None
    
    def start(self):
        """Запускает менеджер потоков"""
        self._stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info(f"Stream manager started for {self.output_name}")
        
    def stop(self):
        """Останавливает менеджер потоков"""
        logger.info(f"Stopping stream manager for {self.output_name}...")
        self._stop_event.set()
        
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=10)
            
        self._stop_ffmpeg()
        logger.info(f"Stream manager stopped for {self.output_name}")
        
    def restart(self):
        """Принудительный перезапуск"""
        logger.info(f"Manual restart requested for {self.output_name}")
        self.restart_count += 1
        self._stop_ffmpeg()
        
    @property
    def is_alive(self) -> bool:
        """Проверяет, жив ли процесс"""
        return self.process is not None and self.process.poll() is None
    
    @property
    def playlist_url(self) -> str:
        """Возвращает URL для доступа к плейлисту"""
        return f"/api/video/hls/{self.output_name}.m3u8"
    
    @property
    def stats(self) -> dict:
        """Возвращает статистику менеджера"""
        healthy, health_message = self._check_playlist_health()
        
        return {
            "output_name": self.output_name,
            "camera_ip": self.camera_ip,
            "stream_type": self.stream_type,
            "is_alive": self.is_alive,
            "start_time": self.start_time,
            "uptime": time.time() - self.start_time if self.start_time else 0,
            "restart_count": self.restart_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "health": {
                "healthy": healthy,
                "message": health_message
            },
            "playlist_exists": os.path.exists(self.playlist_path),
            "playlist_size": os.path.getsize(self.playlist_path) if os.path.exists(self.playlist_path) else 0,
            "log_size": os.path.getsize(self.log_file) if os.path.exists(self.log_file) else 0
        }