#!/usr/bin/env python3
"""
OpenIPC Bridge Addon - Основной сервер
Версия: 1.3.7
Описание: Управление камерами, HLS стриминг, OSD, QR-сканер, TTS, Запись видео
"""


# ==================== БЛОК 0: ИМПОРТЫ ====================

import sys
import os
import hashlib  # Добавлен для генерации ID снимков
import json
import logging
import time
import threading
import subprocess
import tempfile
import base64
import re
import yaml
import glob
import shutil
import socket
import signal
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from collections import deque

from flask import Flask, request, jsonify, render_template, send_from_directory, Response, redirect
import requests

# Импорт модуля записи и менеджера конфигурации
from recording_api import init_recording_api
from config_manager import get_config_manager

# ==================== БЛОК 1: КОНФИГУРАЦИЯ И КОНСТАНТЫ ====================

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# URL для доступа к Home Assistant через Supervisor
HASS_URL = os.environ.get('HASS_URL', 'http://supervisor/core')
SUPERVISOR_TOKEN = os.environ.get('SUPERVISOR_TOKEN', '')
HASS_TOKEN = os.environ.get('HASSIO_TOKEN', SUPERVISOR_TOKEN)

# Пути к скриптам генерации TTS
TTS_GENERATE_SCRIPT = "/app/tts_generate_openipc.sh"
TTS_GENERATE_BEWARD_SCRIPT = "/app/tts_generate.sh"
TTS_GENERATE_RHVoice_SCRIPT = "/app/tts_generate_rhvoice.sh"

# Файлы конфигурации
QR_DEBUG_FILE = "/config/qr_debug.log"
TRANSLATIONS_DIR = "/app/translations"

# Директории для записи
RECORDINGS_DIR = "/config/www/recordings"
EXPORTS_DIR = "/config/www/exports"
SNAPSHOTS_DIR = "/config/www/snapshots"

# Создаем директории для записей
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
os.makedirs(os.path.join(RECORDINGS_DIR, "thumbnails"), exist_ok=True)

# Разрешенные расширения файлов для менеджера
ALLOWED_EXTENSIONS = {'.py', '.sh', '.html', '.yaml', '.yml', '.txt', '.json', '.md'}
WORKING_DIR = '/app'  # Основная директория аддона

# Конфигурация камер по умолчанию (только для RTSP путей)
CAMERA_PATHS = {
    "192.168.1.4": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.5": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.8": {"main": "/stream=0", "sub": None},  # Нет суб-потока
    "192.168.1.10": {"main": "/av0_0", "sub": "/av0_1"},  # Beward
    "192.168.1.40": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.55": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.61": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.75": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.91": {"main": "/stream=0", "sub": "/stream=1"},
    "192.168.1.106": {"main": "/stream=0", "sub": "/stream=1"},
}

# ==================== ИНИЦИАЛИЗАЦИЯ МЕНЕДЖЕРА КОНФИГУРАЦИИ ====================

# Создаем глобальный менеджер конфигурации
config_manager = get_config_manager()
config = config_manager.config  # для обратной совместимости

# Глобальные переменные состояния (не связанные с конфигом)
state = {"started_at": datetime.now().isoformat(), "requests": 0}
scan_jobs: Dict[str, dict] = {}
stream_managers: Dict[str, object] = {}
camera_status_cache = {}
camera_status_cache_time = {}
CACHE_TTL = 30  # секунд
debug_counter = 0
stop_event = threading.Event()


# ==================== БЛОК 1.5: STREAM MONITOR CLASSES ====================

class StreamHealth:
    """Класс для хранения состояния здоровья потока"""
    
    def __init__(self, manager):
        self.manager = manager
        self.restart_count = 0
        self.error_count = 0
        self.last_restart = 0
        self.last_segment_time = 0
        self.segment_history = deque(maxlen=10)
        self.error_history = deque(maxlen=20)
        self.last_check = time.time()
        self.status = "unknown"
        self.consecutive_failures = 0
        self.recovery_attempts = 0
        self.max_recovery_attempts = 5
        self.backoff_time = 1
        
    def record_success(self):
        self.consecutive_failures = 0
        self.recovery_attempts = 0
        self.backoff_time = 1
        self.status = "healthy"
        self.last_check = time.time()
        
    def record_error(self, error_msg: str):
        self.consecutive_failures += 1
        self.error_count += 1
        self.error_history.append({
            'time': time.time(),
            'error': error_msg
        })
        self.status = "unhealthy"
        
    def should_restart(self) -> bool:
        if self.consecutive_failures >= 3:
            return True
        if self.manager.process and self.manager.process.poll() is not None:
            return True
        if self.last_segment_time > 0:
            age = time.time() - self.last_segment_time
            if age > 30:
                return True
        return False
        
    def get_recovery_delay(self) -> float:
        self.recovery_attempts += 1
        delay = self.backoff_time * (2 ** (self.recovery_attempts - 1))
        return min(delay, 60)


class StreamMonitor(threading.Thread):
    def __init__(self, stream_managers: Dict, stop_event: threading.Event):
        super().__init__(name="stream_monitor")
        self.stream_managers = stream_managers
        self.stop_event = stop_event
        self.health_stats: Dict[str, StreamHealth] = {}
        self.daemon = True
        
        self.global_stats = {
            'total_restarts': 0,
            'total_errors': 0,
            'uptime': time.time(),
            'monitored_streams': 0
        }
        
        self.check_interval = 2
        self.playlist_check_interval = 5
        self.stats_log_interval = 300
        self.playlist_cache = {}
        
    def run(self):
        logger.info("🚀 Stream Monitor started")
        last_stats_log = time.time()
        last_playlist_check = time.time()
        
        while not self.stop_event.is_set():
            try:
                current_time = time.time()
                self._check_streams()
                
                if current_time - last_playlist_check >= self.playlist_check_interval:
                    self._check_playlists()
                    last_playlist_check = current_time
                
                if current_time - last_stats_log >= self.stats_log_interval:
                    self._log_stats()
                    last_stats_log = current_time
                
                self._cleanup_stale()
                
            except Exception as e:
                logger.error(f"Error in Stream Monitor: {e}")
            
            self.stop_event.wait(self.check_interval)
        
        logger.info("🛑 Stream Monitor stopped")
    
    def _check_streams(self):
        for name, manager in list(self.stream_managers.items()):
            try:
                if name not in self.health_stats:
                    self.health_stats[name] = StreamHealth(manager)
                
                health = self.health_stats[name]
                
                if manager.process is None:
                    health.record_error("Process not running")
                    if health.should_restart():
                        self._restart_stream(name, manager, health)
                    continue
                    
                if manager.process.poll() is not None:
                    health.record_error(f"Process died with code {manager.process.returncode}")
                    if health.should_restart():
                        self._restart_stream(name, manager, health)
                    continue
                
                segments = self._get_segments(manager.hls_dir)
                if segments:
                    latest_segment = max(segments, key=lambda s: os.path.getmtime(s))
                    segment_time = os.path.getmtime(latest_segment)
                    
                    if segment_time > health.last_segment_time:
                        health.last_segment_time = segment_time
                        health.segment_history.append(segment_time)
                    
                    age = time.time() - segment_time
                    if age > 30:
                        health.record_error(f"No new segments for {age:.0f}s")
                        if health.should_restart():
                            self._restart_stream(name, manager, health)
                        continue
                
                health.record_success()
                
            except Exception as e:
                logger.error(f"Error checking stream {name}: {e}")
    
    def _check_playlists(self):
        for name, manager in list(self.stream_managers.items()):
            try:
                playlist_path = manager.playlist_path
                if not os.path.exists(playlist_path):
                    continue
                
                mtime = os.path.getmtime(playlist_path)
                age = time.time() - mtime
                
                self.playlist_cache[name] = {
                    'mtime': mtime,
                    'age': age,
                    'size': os.path.getsize(playlist_path)
                }
                
                if age > 60:
                    logger.warning(f"Playlist for {name} stale ({age:.0f}s old)")
                    
            except Exception as e:
                logger.error(f"Error checking playlist {name}: {e}")
    
    def _restart_stream(self, name: str, manager, health: StreamHealth):
        try:
            delay = health.get_recovery_delay()
            logger.warning(f"Restarting stream {name} (attempt {health.recovery_attempts}/{health.max_recovery_attempts}, delay={delay:.1f}s)")
            
            if health.recovery_attempts > health.max_recovery_attempts:
                logger.error(f"Stream {name} failed to recover after {health.max_recovery_attempts} attempts")
                return
            
            manager.stop()
            time.sleep(1)
            success = manager.start()
            
            if success:
                health.record_success()
                health.restart_count += 1
                self.global_stats['total_restarts'] += 1
                logger.info(f"✅ Stream {name} restarted successfully")
            else:
                health.record_error("Failed to restart")
                logger.error(f"❌ Failed to restart stream {name}")
                
        except Exception as e:
            logger.error(f"Error restarting stream {name}: {e}")
    
    def _cleanup_stale(self):
        current_time = time.time()
        for name, manager in list(self.stream_managers.items()):
            try:
                if manager.process is None or manager.process.poll() is not None:
                    if name in self.health_stats:
                        health = self.health_stats[name]
                        if current_time - health.last_restart > 300:
                            logger.info(f"Removing dead stream manager for {name}")
                            del self.stream_managers[name]
                            del self.health_stats[name]
            except Exception as e:
                logger.error(f"Error cleaning up {name}: {e}")
    
    def _get_segments(self, hls_dir: str) -> list:
        try:
            if os.path.exists(hls_dir):
                return [os.path.join(hls_dir, f) for f in os.listdir(hls_dir) 
                       if f.startswith('segment_') and f.endswith('.ts')]
        except Exception:
            pass
        return []
    
    def _read_last_log_lines(self, log_file: str, num_lines: int) -> str:
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    return ''.join(lines[-num_lines:])
        except Exception:
            pass
        return "Log file not found"
    
    def _log_stats(self):
        total_streams = len(self.stream_managers)
        healthy = 0
        unhealthy = 0
        
        for name, health in self.health_stats.items():
            if health.status == "healthy":
                healthy += 1
            else:
                unhealthy += 1
        
        logger.info("=" * 60)
        logger.info("📊 Stream Monitor Statistics")
        logger.info("=" * 60)
        logger.info(f"Total streams: {total_streams}")
        logger.info(f"Healthy: {healthy}")
        logger.info(f"Unhealthy: {unhealthy}")
        logger.info(f"Total restarts: {self.global_stats['total_restarts']}")
        logger.info(f"Total errors: {self.global_stats['total_errors']}")
        logger.info(f"Uptime: {self._format_uptime()}")
        
        if unhealthy > 0:
            logger.info("\n❌ Unhealthy streams:")
            for name, health in self.health_stats.items():
                if health.status != "healthy":
                    logger.info(f"  - {name}: restarts={health.restart_count}, errors={health.error_count}")
        
        logger.info("=" * 60)
    
    def _format_uptime(self) -> str:
        uptime = time.time() - self.global_stats['uptime']
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    
    def get_stream_status(self, name: str = None) -> dict:
        if name:
            if name in self.health_stats:
                health = self.health_stats[name]
                return {
                    'name': name,
                    'status': health.status,
                    'restarts': health.restart_count,
                    'errors': health.error_count,
                    'last_check': health.last_check,
                    'consecutive_failures': health.consecutive_failures,
                    'recovery_attempts': health.recovery_attempts,
                    'segment_count': len(health.segment_history),
                    'last_segment_time': health.last_segment_time
                }
            return None
        else:
            status = {}
            for name, health in self.health_stats.items():
                status[name] = {
                    'status': health.status,
                    'restarts': health.restart_count,
                    'errors': health.error_count
                }
            return status


def init_stream_monitor(app, stream_managers, stop_event):
    monitor = StreamMonitor(stream_managers, stop_event)
    monitor.start()
    app.stream_monitor = monitor
    
    @app.route('/api/monitor/status')
    def monitor_status():
        return jsonify({
            'success': True,
            'monitor_running': monitor.is_alive(),
            'global_stats': monitor.global_stats,
            'streams': monitor.get_stream_status(),
            'playlist_cache': monitor.playlist_cache
        })
    
    @app.route('/api/monitor/stream/<stream_name>')
    def monitor_stream_status(stream_name):
        status = monitor.get_stream_status(stream_name)
        if status:
            return jsonify({'success': True, 'stream': status})
        return jsonify({'success': False, 'error': 'Stream not found'}), 404
    
    @app.route('/api/monitor/restart/<stream_name>', methods=['POST'])
    def monitor_restart_stream(stream_name):
        if stream_name in stream_managers:
            manager = stream_managers[stream_name]
            if stream_name in monitor.health_stats:
                health = monitor.health_stats[stream_name]
                monitor._restart_stream(stream_name, manager, health)
                return jsonify({'success': True, 'message': f'Restarting {stream_name}'})
        return jsonify({'success': False, 'error': 'Stream not found'}), 404
    
    return monitor


# ==================== БЛОК 2: STREAM MANAGER ====================

class StreamManager:
    """Управляет FFmpeg процессами для HLS стриминга с максимальной стабильностью"""
    
    def __init__(self, camera_ip, username, password, stream_type='main', on_status_change=None):
        self.camera_ip = camera_ip
        self.username = username
        self.password = password
        self.stream_type = stream_type
        self.output_name = f"{camera_ip}_{stream_type}"
        self.on_status_change = on_status_change
        
        # Проверяем доступность запрошенного потока
        if camera_ip in CAMERA_PATHS:
            if CAMERA_PATHS[camera_ip].get(stream_type) is None:
                logging.warning(f"⚠️ {stream_type} stream not available for {camera_ip}, using main")
                self.stream_type = "main"
                self.output_name = f"{camera_ip}_main"
        
        self.hls_dir = f"/tmp/hls/{self.output_name}"
        self.log_file = f"{self.hls_dir}/ffmpeg.log"
        self.playlist_path = f"{self.hls_dir}/playlist.m3u8"
        
        # Создаем директорию и устанавливаем права
        try:
            os.makedirs(self.hls_dir, exist_ok=True)
            os.chmod(self.hls_dir, 0o777)
            logging.info(f"✅ Created HLS directory with 777 permissions: {self.hls_dir}")
            
            # Проверяем, можем ли мы писать в директорию
            test_file = f"{self.hls_dir}/write_test.tmp"
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            logging.info(f"✅ Write test successful for {self.hls_dir}")
            
        except Exception as e:
            logging.error(f"❌ Failed to create/write to HLS dir: {e}")
        
        # Определяем путь к потоку
        if camera_ip in CAMERA_PATHS:
            stream_path = CAMERA_PATHS[camera_ip].get(self.stream_type, "/stream=0")
        else:
            # По умолчанию для неизвестных камер
            stream_path = '/stream=0' if self.stream_type == 'main' else '/stream=1'
        
        self.rtsp_url = f"rtsp://{username}:{password}@{camera_ip}:554{stream_path}"
        
        self.process = None
        self.monitor_thread = None
        self._stop_event = threading.Event()
        self.start_time = None
        self.restart_count = 0
        self.error_count = 0
        self.last_error = None
        self.ffmpeg_output = ""
        self.last_segment_time = 0
        self.segment_count = 0
    
    def _get_ffmpeg_cmd(self, rtsp_transport='tcp'):
        """Команда FFmpeg с улучшенными параметрами для максимальной стабильности"""
        
        # Обновляем URL с учетом возможного изменения stream_type
        if self.camera_ip in CAMERA_PATHS:
            stream_path = CAMERA_PATHS[self.camera_ip].get(self.stream_type, "/stream=0")
        else:
            stream_path = '/stream=0' if self.stream_type == 'main' else '/stream=1'
        
        self.rtsp_url = f"rtsp://{self.username}:{self.password}@{self.camera_ip}:554{stream_path}"
        logging.info(f"📹 Using RTSP URL: {self.rtsp_url.replace(self.password, '****')}")
        
        # Расширенные параметры для нестабильных соединений
        input_options = [
            '-rtsp_transport', rtsp_transport,
            '-timeout', '10000000',  # Увеличим таймаут до 10 сек
            '-buffer_size', '4096000',  # Увеличим буфер до 4MB
            '-max_delay', '2000000',  # Увеличим задержку до 2 сек
            '-reorder_queue_size', '16000',  # Увеличим очередь
            '-analyzeduration', '10000000',  # Анализ потока 10 сек
            '-probesize', '10000000',  # Размер зонда 10MB
            '-flags', '+low_delay+igndts',  # Низкая задержка + игнор DTS
            '-fflags', '+genpts+discardcorrupt+igndts',  # Генерация PTS, игнор ошибок
            '-strict', 'experimental',  # Экспериментальный режим
            '-err_detect', 'ignore_err',  # Игнорировать ошибки
            '-discard', 'nokey',  # Не отбрасывать ключевые кадры
            '-skip_frame', 'nokey',  # Пропускать не-ключевые кадры при проблемах
            '-r', '15',  # Ограничим FPS до 15 для стабильности
        ]
        
        # Абсолютные пути к файлам
        segment_path = f"{self.hls_dir}/segment_%03d.ts"
        playlist_path_abs = self.playlist_path
        
        # Команда с оптимизированными параметрами кодирования
        cmd = [
            'ffmpeg',
            *input_options,
            '-i', self.rtsp_url,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',  # Минимальная нагрузка
            '-tune', 'zerolatency',
            '-crf', '28',  # Чуть ниже качество для стабильности
            '-maxrate', '800k',  # Ограничим битрейт
            '-bufsize', '1600k',
            '-vsync', '1',  # Синхронизация видео
            '-async', '1',  # Синхронизация аудио
            '-c:a', 'aac',
            '-ar', '16000',  # Понизим частоту аудио
            '-ac', '1',  # Моно
            '-b:a', '24k',  # Низкий битрейт аудио
            '-f', 'hls',
            '-hls_time', '3',  # Длительность сегмента 3 сек
            '-hls_list_size', '4',  # 4 сегмента в списке
            '-hls_flags', 'delete_segments+omit_endlist+split_by_time',
            '-hls_segment_filename', segment_path,
            '-hls_playlist_type', 'event',
            '-hls_start_number_source', 'datetime',  # Нумерация по времени
            '-hls_wrap', '10',  # Перезапись после 10 сегментов
            '-master_pl_name', 'master.m3u8',  # Основной плейлист
            '-y',
            playlist_path_abs
        ]
        
        logging.debug(f"FFmpeg command: {' '.join(cmd)}")
        return cmd
    
    def _start_ffmpeg(self):
        """Запускает FFmpeg процесс с захватом вывода"""
        try:
            # Проверяем, что поток существует (специальная обработка для 192.168.1.8)
            if self.stream_type == "sub" and self.camera_ip == "192.168.1.8":
                logging.warning(f"⚠️ Camera {self.camera_ip} has no sub stream, using main")
                self.stream_type = "main"
                self.output_name = f"{self.camera_ip}_main"
                self.hls_dir = f"/tmp/hls/{self.output_name}"
                self.playlist_path = f"{self.hls_dir}/playlist.m3u8"
                os.makedirs(self.hls_dir, exist_ok=True)
                os.chmod(self.hls_dir, 0o777)
            
            cmd = self._get_ffmpeg_cmd()
            cmd_str = ' '.join(cmd)
            logging.info(f"🎬 Starting FFmpeg for {self.output_name}")
            logging.info(f"📋 Command: {cmd_str}")
            
            # Создаем директорию для лога
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            
            # Запускаем процесс и захватываем вывод
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            self.start_time = time.time()
            self.last_segment_time = time.time()
            logging.info(f"✅ FFmpeg started for {self.output_name} with PID {self.process.pid}")
            
            # Запускаем поток для чтения вывода
            def read_output():
                while self.process and self.process.poll() is None:
                    try:
                        # Читаем stderr (туда FFmpeg пишет логи)
                        line = self.process.stderr.readline()
                        if line:
                            self.ffmpeg_output += line
                            # Логируем важные сообщения
                            if 'error' in line.lower() or 'warning' in line.lower():
                                logging.warning(f"FFmpeg [{self.output_name}]: {line.strip()}")
                            else:
                                logging.debug(f"FFmpeg [{self.output_name}]: {line.strip()}")
                            
                            # Отслеживаем создание сегментов
                            if 'segment' in line.lower() and 'writing' in line.lower():
                                self.segment_count += 1
                                self.last_segment_time = time.time()
                    except:
                        break
            
            threading.Thread(target=read_output, daemon=True).start()
            
            if self.on_status_change:
                self.on_status_change(self.output_name, "running", self.restart_count)
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logging.error(f"❌ Failed to start FFmpeg for {self.output_name}: {e}")
            self.process = None
            return False
    
    def _stop_ffmpeg(self, force=False):
        """Останавливает FFmpeg процесс с гарантированным завершением"""
        if self.process is None:
            return
        
        logging.info(f"🛑 Stopping FFmpeg for {self.output_name}...")
        
        # Сначала пытаемся gracefully завершить
        try:
            if not force:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                    logging.info(f"✅ FFmpeg for {self.output_name} terminated gracefully")
                except subprocess.TimeoutExpired:
                    logging.warning(f"⚠️ FFmpeg for {self.output_name} didn't exit, killing...")
                    self.process.kill()
                    self.process.wait(timeout=3)
            else:
                self.process.kill()
                self.process.wait(timeout=2)
                logging.info(f"✅ FFmpeg for {self.output_name} killed")
        except Exception as e:
            logging.error(f"❌ Error stopping FFmpeg: {e}")
        finally:
            # Сохраняем последние строки вывода перед закрытием
            try:
                stdout, stderr = self.process.communicate(timeout=1)
                if stdout:
                    self.ffmpeg_output += stdout
                if stderr:
                    self.ffmpeg_output += stderr
            except:
                pass
            
            # Записываем весь вывод в лог-файл
            try:
                with open(self.log_file, 'w') as f:
                    f.write(self.ffmpeg_output)
                logging.info(f"📝 FFmpeg output saved to {self.log_file}")
            except Exception as e:
                logging.error(f"Failed to save log: {e}")
            
            self.process = None
    
    def _check_playlist_health(self):
        """Проверяет здоровье HLS плейлиста с расширенной диагностикой"""
        if not os.path.exists(self.playlist_path):
            return False, "Playlist not found"
            
        try:
            size = os.path.getsize(self.playlist_path)
            if size == 0:
                return False, "Playlist is empty"
            
            # Проверяем содержимое плейлиста
            with open(self.playlist_path, 'r') as f:
                content = f.read()
                
            # Проверяем наличие EXTINF (сегменты)
            if '#EXTINF' not in content:
                return False, "No segments in playlist"
            
            # Проверяем, что плейлист обновлялся последние 15 секунд
            mtime = os.path.getmtime(self.playlist_path)
            age = time.time() - mtime
            if age > 15:
                return False, f"Playlist stale (last update: {age:.1f}s ago)"
            
            # Проверяем наличие сегментов на диске
            segments = glob.glob(f"{self.hls_dir}/segment_*.ts")
            if not segments:
                return False, "No segment files found"
            
            # Проверяем возраст последнего сегмента
            latest_segment = max(segments, key=os.path.getmtime)
            segment_age = time.time() - os.path.getmtime(latest_segment)
            if segment_age > 20:
                return False, f"Latest segment stale (age: {segment_age:.1f}s)"
            
            # Проверяем размер последнего сегмента
            segment_size = os.path.getsize(latest_segment)
            if segment_size < 1000:  # Меньше 1KB - подозрительно
                logging.warning(f"Segment {os.path.basename(latest_segment)} is too small: {segment_size} bytes")
            
            return True, f"OK (segments: {len(segments)}, age: {age:.1f}s)"
            
        except Exception as e:
            return False, f"Error checking playlist: {e}"
    
    def _monitor(self):
        """Фоновый поток мониторинга с улучшенной логикой"""
        logging.info(f"🔍 Monitor started for {self.output_name}")
        
        consecutive_failures = 0
        playlist_check_interval = 2
        process_check_interval = 5
        last_playlist_check = 0
        last_process_check = 0
        last_segment_check = 0
        
        while not self._stop_event.is_set():
            now = time.time()
            
            # 1. Проверка процесса FFmpeg
            if now - last_process_check >= process_check_interval:
                last_process_check = now
                
                # Если процесс не запущен, запускаем
                if self.process is None:
                    logging.warning(f"⚠️ FFmpeg not running for {self.output_name}, starting...")
                    if self._start_ffmpeg():
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        if consecutive_failures > 2:
                            logging.error(f"💥 Failed to start FFmpeg for {self.output_name} after 3 attempts")
                            break
                
                # Проверяем, жив ли процесс
                elif self.process.poll() is not None:
                    self.error_count += 1
                    self.last_error = f"Process died with code {self.process.returncode}"
                    logging.error(f"❌ FFmpeg for {self.output_name} died: {self.last_error}")
                    
                    # Читаем последние строки лога
                    log_tail = self._read_last_log_lines(50)
                    logging.error(f"Last logs:\n{log_tail}")
                    
                    self.restart_count += 1
                    self._stop_ffmpeg(force=True)
                    
                    if self.on_status_change:
                        self.on_status_change(self.output_name, "restarting", self.restart_count)
                    
                    consecutive_failures += 1
                    if consecutive_failures > 5:
                        logging.error(f"💥 Too many consecutive failures for {self.output_name}, waiting 60s...")
                        self._stop_event.wait(60)
            
            # 2. Проверка HLS плейлиста
            if now - last_playlist_check >= playlist_check_interval:
                last_playlist_check = now
                
                if self.process is not None:
                    healthy, message = self._check_playlist_health()
                    
                    if not healthy:
                        consecutive_failures += 1
                        logging.warning(f"HLS health check failed for {self.output_name}: {message} ({consecutive_failures}/3)")
                        
                        # Дополнительная диагностика
                        if 'No segment files' in message:
                            # Если нет сегментов, возможно проблема с RTSP
                            logging.warning("No segments found, checking RTSP connection...")
                            # Можно попробовать перезапустить с другим транспортом
                            if consecutive_failures >= 2:
                                logging.info("Trying to restart with UDP transport...")
                                self._stop_ffmpeg(force=True)
                                # TODO: можно реализовать смену транспорта
                        
                        if consecutive_failures >= 3:
                            logging.error(f"Too many HLS failures for {self.output_name}, restarting FFmpeg...")
                            self.restart_count += 1
                            self._stop_ffmpeg(force=True)
                            consecutive_failures = 0
                            if self.on_status_change:
                                self.on_status_change(self.output_name, "restarting", self.restart_count)
                    else:
                        # Сброс счетчика при успешной проверке
                        if consecutive_failures > 0:
                            logging.info(f"HLS health restored for {self.output_name}")
                        consecutive_failures = 0
                        if self.on_status_change and self.restart_count > 0:
                            self.on_status_change(self.output_name, "running", self.restart_count)
            
            # 3. Проверка появления новых сегментов
            if now - last_segment_check >= 10:
                last_segment_check = now
                if time.time() - self.last_segment_time > 30:
                    logging.warning(f"No new segments for 30 seconds for {self.output_name}")
                    if consecutive_failures < 2:
                        consecutive_failures += 1
            
            # Небольшая пауза, чтобы не нагружать CPU
            self._stop_event.wait(0.5)
        
        logging.info(f"🛑 Monitor stopped for {self.output_name}")
    
    def _read_last_log_lines(self, num_lines=20):
        """Читает последние строки из лога FFmpeg"""
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r') as f:
                    lines = f.readlines()
                    return ''.join(lines[-num_lines:])
        except Exception as e:
            return f"Error reading log: {e}"
        return "Log file not found"
    
    def start(self):
        """Запускает менеджер потоков"""
        logging.info(f"🚀 Starting stream manager for {self.output_name}")
        self._stop_event.clear()
        if self._start_ffmpeg():
            self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self.monitor_thread.start()
            return True
        return False
    
    def stop(self):
        """Останавливает менеджер потоков"""
        logging.info(f"🛑 Stopping stream manager for {self.output_name}")
        self._stop_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=10)
        self._stop_ffmpeg(force=True)
    
    def restart(self):
        """Принудительный перезапуск"""
        logging.info(f"🔄 Manual restart requested for {self.output_name}")
        self.restart_count += 1
        self._stop_ffmpeg(force=True)
    
    @property
    def is_alive(self):
        return self.process is not None and self.process.poll() is None
    
    @property
    def playlist_url(self):
        return f"/api/video/hls/{self.output_name}.m3u8"
    
    @property
    def stats(self):
        """Возвращает расширенную статистику менеджера"""
        # Проверяем наличие сегментов
        segments = []
        segment_sizes = []
        if os.path.exists(self.hls_dir):
            segments = glob.glob(f"{self.hls_dir}/segment_*.ts")
            segment_sizes = [os.path.getsize(s) for s in segments]
        
        # Читаем содержимое плейлиста для отладки
        playlist_content = ""
        if os.path.exists(self.playlist_path):
            try:
                with open(self.playlist_path, 'r') as f:
                    playlist_content = f.read()
            except:
                pass
        
        healthy, health_message = self._check_playlist_health()
        
        return {
            "output_name": self.output_name,
            "camera_ip": self.camera_ip,
            "stream_type": self.stream_type,
            "is_alive": self.is_alive,
            "uptime": time.time() - self.start_time if self.start_time else 0,
            "restart_count": self.restart_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "playlist_exists": os.path.exists(self.playlist_path),
            "playlist_size": os.path.getsize(self.playlist_path) if os.path.exists(self.playlist_path) else 0,
            "playlist_content": playlist_content[:500] if playlist_content else "",
            "segment_count": len(segments),
            "segment_sizes": [f"{s/1024:.1f}KB" for s in segment_sizes[-5:]],
            "segments": [os.path.basename(s) for s in segments[-5:]],
            "last_segment_time": self.last_segment_time,
            "time_since_last_segment": time.time() - self.last_segment_time if self.last_segment_time else -1,
            "log_exists": os.path.exists(self.log_file),
            "log_size": os.path.getsize(self.log_file) if os.path.exists(self.log_file) else 0,
            "health": {
                "healthy": healthy,
                "message": health_message
            }
        }


# ==================== БЛОК 3: FLASK ПРИЛОЖЕНИЕ И БАЗОВЫЕ ЭНДПОИНТЫ ====================

app = Flask(__name__)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_camera_config(camera_ip: str) -> Optional[Dict]:
    """Получить конфигурацию камеры по IP (для обратной совместимости)"""
    return config_manager.get_camera(camera_ip)


def get_camera_config_by_name(camera_name: str) -> Optional[Dict]:
    """Получить конфигурацию камеры по имени"""
    return config_manager.get_camera_by_name(camera_name)


def get_cameras_list():
    """Получить список всех камер с их статусом"""
    return config_manager.get_cameras_list(include_status=True)


def get_camera_recording_settings(camera_ip):
    """Получить настройки записи для камеры из конфигурации"""
    try:
        cam_config = config_manager.get_camera(camera_ip)
        
        if not cam_config:
            logger.warning(f"No camera config found for {camera_ip}")
            return None
            
        # Настройки по умолчанию
        default = {
            'mode': 'continuous',
            'enabled': True,
            'segment_duration': 300,
            'archive_depth': 7,
            'quality': 'medium',
            'detect_motion': True,
            'detect_qr': True
        }
        
        # Получаем настройки из конфига
        recording_settings = cam_config.get('recording', {})
        detection_settings = cam_config.get('detection', {})
        
        settings = {
            'mode': recording_settings.get('mode', default['mode']),
            'enabled': recording_settings.get('enabled', default['enabled']),
            'segment_duration': recording_settings.get('segment_duration', default['segment_duration']),
            'archive_depth': recording_settings.get('archive_depth', default['archive_depth']),
            'quality': recording_settings.get('quality', default['quality']),
            'format': recording_settings.get('format', 'mp4'),
            'detect_motion': detection_settings.get('motion', default['detect_motion']),
            'detect_qr': detection_settings.get('qr', default['detect_qr'])
        }
        
        logger.debug(f"Recording settings for {camera_ip}: {settings}")
        return settings
        
    except Exception as e:
        logger.error(f"Error getting recording settings for {camera_ip}: {e}")
        return None


def get_camera_type(camera_ip):
    """Получить тип камеры из конфигурации"""
    try:
        cam_config = config_manager.get_camera(camera_ip)
        if cam_config:
            return cam_config.get('type', 'openipc')
    except Exception as e:
        logger.error(f"Error getting camera type for {camera_ip}: {e}")
    return 'openipc'


def get_camera_credentials(camera_ip):
    """Получить логин/пароль камеры"""
    try:
        cam_config = config_manager.get_camera(camera_ip)
        if cam_config:
            return cam_config.get('username', 'root'), cam_config.get('password', '12345')
    except Exception as e:
        logger.error(f"Error getting credentials for {camera_ip}: {e}")
    return 'root', '12345'


def check_camera_online(camera_ip, port=80, timeout=1):
    """Проверить, доступна ли камера по сети"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((camera_ip, port))
        sock.close()
        return result == 0
    except Exception as e:
        logger.debug(f"Error checking camera {camera_ip}: {e}")
        return False


def get_all_cameras_status():
    """Получить статус всех камер (с кэшированием)"""
    global camera_status_cache, camera_status_cache_time
    
    current_time = time.time()
    
    # Проверяем кэш
    if 'all' in camera_status_cache_time:
        cache_age = current_time - camera_status_cache_time['all']
        if cache_age < CACHE_TTL:
            logger.debug(f"Returning cached camera status (age: {cache_age:.1f}s)")
            return camera_status_cache['all']
    
    # Получаем свежие статусы
    cameras = []
    for cam in config_manager.get_cameras_list():
        online = check_camera_online(cam['ip'], 80)
        cam_copy = cam.copy()
        cam_copy['online'] = online
        cameras.append(cam_copy)
    
    # Сохраняем в кэш
    camera_status_cache['all'] = cameras
    camera_status_cache_time['all'] = current_time
    
    logger.debug(f"Updated camera status: {len(cameras)} cameras, {sum(1 for c in cameras if c['online'])} online")
    return cameras


def write_qr_debug(msg):
    """Запись в отладочный файл QR"""
    if not config_manager.config['logging'].get('debug_qr', True):
        return
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(QR_DEBUG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{timestamp}: {msg}\n")
    except Exception as e:
        logger.error(f"Failed to write QR debug: {e}")


def load_translations(lang='en'):
    """Загрузить переводы"""
    try:
        trans_file = os.path.join(TRANSLATIONS_DIR, f"{lang}.yaml")
        if os.path.exists(trans_file):
            with open(trans_file, 'r', encoding='utf-8') as f:
                translations = yaml.safe_load(f)
                logger.debug(f"✅ Loaded translations for '{lang}'")
                return translations
        else:
            logger.debug(f"Translations file not found: {trans_file}")
            return {}
    except Exception as e:
        logger.error(f"❌ Failed to load translations: {e}")
        return {}


# ==================== Фоновые задачи ====================

def cleanup_stale_streams():
    """Периодическая очистка зависших потоков"""
    while True:
        time.sleep(60)
        current_time = time.time()
        
        for name, manager in list(stream_managers.items()):
            # Проверяем, не завис ли процесс
            if manager.is_alive:
                # Проверяем время последнего обновления плейлиста
                if os.path.exists(manager.playlist_path):
                    mtime = os.path.getmtime(manager.playlist_path)
                    if current_time - mtime > 30:  # Нет обновлений 30 секунд
                        logger.warning(f"Stream {name} appears stale, restarting...")
                        manager.restart()
            else:
                # Процесс мертв, но менеджер еще в словаре - удаляем
                logger.info(f"Removing dead stream manager for {name}")
                del stream_managers[name]

# Запускаем фоновую задачу очистки
threading.Thread(target=cleanup_stale_streams, daemon=True).start()

# ==================== Базовые эндпоинты (СТРАНИЦЫ) ====================

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html')


@app.route('/health')
def health():
    return jsonify({"status": "healthy"})


@app.route('/config')
def config_page():
    """Страница конфигурации камер"""
    return render_template('config.html')


@app.route('/osd')
def osd_page():
    """Страница настройки OSD"""
    return render_template('osd.html')


@app.route('/qr')
def qr_page():
    """Страница QR-сканера и генератора"""
    return render_template('qr.html')


@app.route('/tts')
def tts_page():
    """Страница настройки TTS"""
    return render_template('tts.html')


@app.route('/video')
def video_page():
    """Страница просмотра видео"""
    return render_template('video.html')


@app.route('/diagnose')
def diagnose_page():
    """Страница диагностики"""
    return render_template('diagnose.html')


@app.route('/files')
def files_page():
    """Страница управления файлами"""
    return render_template('files.html')


@app.route('/archive')
def archive_page():
    """Страница архива записей"""
    return render_template('archive.html')


@app.route('/snapshots')
def snapshots_page():
    """Страница просмотра снимков"""
    return render_template('snapshots.html')


@app.route('/storage')
def storage_page():
    """Страница управления хранилищами"""
    return render_template('storage.html')


@app.route('/notifications')
def notifications_page():
    """Страница уведомлений с настройками Telegram"""
    return render_template('notifications.html', config=config_manager.config)


@app.route('/resources')
def resources_page():
    """Страница мониторинга ресурсов"""
    return render_template('resources.html')


@app.route('/monitor')
def monitor_page():
    """Страница мониторинга камер"""
    return render_template('monitor.html')


@app.route('/test')
def test_page():
    """Тестовая страница с MJPEG и диагностикой"""
    return render_template('test.html')


# ===== ЭНДПОИНТЫ ДЛЯ РАЗДАЧИ ФАЙЛОВ =====
@app.route('/recordings/<path:filename>')
def serve_recording(filename):
    """Раздача файлов записей"""
    try:
        return send_from_directory('/config/www/recordings', filename)
    except Exception as e:
        logger.error(f"Error serving recording {filename}: {e}")
        return "File not found", 404


@app.route('/exports/<path:filename>')
def serve_export(filename):
    """Раздача экспортированных файлов"""
    try:
        return send_from_directory('/config/www/exports', filename)
    except Exception as e:
        logger.error(f"Error serving export {filename}: {e}")
        return "File not found", 404


@app.route('/snapshots/<path:filename>')
def serve_snapshot(filename):
    """Раздача снимков"""
    try:
        return send_from_directory('/config/www/snapshots', filename)
    except Exception as e:
        logger.error(f"Error serving snapshot {filename}: {e}")
        return "File not found", 404
        
        
# ==================== БЛОК 4: API ДЛЯ СТАТУСА И СТАТИСТИКИ ====================

@app.route('/api/status')
def api_status():
    """Общая статистика сервиса"""
    uptime = datetime.now() - datetime.fromisoformat(state["started_at"])
    hours = uptime.total_seconds() // 3600
    minutes = (uptime.total_seconds() % 3600) // 60
    
    return jsonify({
        "uptime": f"{int(hours)}ч {int(minutes)}м",
        "requests": state["requests"],
        "cameras_count": len(config_manager.config['cameras']),
        "active_scans": len([j for j in scan_jobs.values() if j['status'] in ['starting', 'running']]),
        "qr_stats": qr_stats
    })


@app.route('/api/cameras/status')
def cameras_status():
    """Статус всех камер с кэшированием"""
    cameras = get_all_cameras_status()
    
    # Добавляем OSD информацию
    for cam in cameras:
        cam_config = config_manager.get_camera(cam['ip'])
        if cam_config:
            cam['osd_enabled'] = cam_config.get('osd', {}).get('enabled', False)
            cam['osd_port'] = cam_config.get('osd', {}).get('port', 9000)
    
    return jsonify({"cameras": cameras})


@app.route('/api/active_jobs')
def active_jobs():
    """Активные задачи"""
    jobs = []
    for scan_id, job in scan_jobs.items():
        if job['status'] in ['starting', 'running']:
            elapsed = time.time() - job['start_time']
            progress = min(100, int((elapsed / job['timeout']) * 100))
            jobs.append({
                "id": scan_id,
                "camera": job['camera_id'],
                "type": "QR Scan",
                "progress": progress,
                "status": job['status'],
                "expected_code": job['expected_code']
            })
    return jsonify({"jobs": jobs})


@app.route('/api/server_time')
def server_time():
    """Серверное время"""
    return jsonify({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route('/api/check_updates')
def check_updates():
    """Проверка обновлений (заглушка)"""
    return jsonify({"message": "Текущая версия актуальна"})


@app.route('/api/camera/<path:camera_ip>/snapshot')
def camera_snapshot(camera_ip):
    """Получить снимок с камеры для предпросмотра"""
    snapshot = capture_snapshot_from_camera(camera_ip)
    if snapshot:
        return Response(snapshot, mimetype='image/jpeg')
    return '', 404


# ==================== БЛОК 5: API ДЛЯ РАБОТЫ С КОНФИГУРАЦИЕЙ ====================

def save_config():
    """Сохранить текущую конфигурацию в файл"""
    global config
    try:
        # Создаем бэкап перед сохранением
        if os.path.exists(CONFIG_FILE):
            backup_file = f"{CONFIG_FILE}.backup"
            shutil.copy2(CONFIG_FILE, backup_file)
            logger.debug(f"✅ Created config backup: {backup_file}")
        
        # Сохраняем конфигурацию
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        logger.info("✅ Configuration saved successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save config: {e}")
        return False


@app.route('/api/config', methods=['GET'])
def get_config_api():
    """Получить текущую конфигурацию"""
    try:
        return jsonify({
            "success": True,
            "config": config
        })
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/save', methods=['POST'])
def save_config_api():
    """Сохранить конфигурацию через API"""
    try:
        new_config = request.json
        
        if not new_config:
            return jsonify({"success": False, "error": "No configuration data provided"}), 400
        
        # Обновляем глобальную конфигурацию
        global config
        config = new_config
        
        # Сохраняем в файл
        if save_config():
            # Обновляем уровень логирования если изменился
            level = config.get('logging', {}).get('level', 'INFO')
            logger.setLevel(getattr(logging, level))
            
            logger.info("✅ Configuration saved via API")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to save config file"}), 500
            
    except Exception as e:
        logger.error(f"❌ Failed to save config via API: {e}")
        logger.exception(e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/reload', methods=['POST'])
def reload_config_api():
    """Перезагрузить конфигурацию из файла"""
    try:
        load_config()
        logger.info("✅ Configuration reloaded from file")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"❌ Failed to reload config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/camera/<camera_ip>', methods=['GET', 'POST'])
def camera_config_api(camera_ip):
    """Получить или обновить конфигурацию конкретной камеры"""
    try:
        if request.method == 'GET':
            # Получить конфигурацию камеры
            cam_config = get_camera_config(camera_ip)
            if cam_config:
                return jsonify({
                    "success": True,
                    "camera": cam_config
                })
            else:
                return jsonify({
                    "success": False,
                    "error": f"Camera {camera_ip} not found"
                }), 404
                
        elif request.method == 'POST':
            # Обновить конфигурацию камеры
            data = request.json
            if not data:
                return jsonify({"success": False, "error": "No data provided"}), 400
            
            # Находим камеру
            for i, cam in enumerate(config['cameras']):
                if cam['ip'] == camera_ip:
                    # Обновляем поля
                    for key, value in data.items():
                        if key in cam:
                            cam[key] = value
                    
                    # Сохраняем изменения
                    if save_config():
                        logger.info(f"✅ Camera {camera_ip} configuration updated")
                        return jsonify({"success": True})
                    else:
                        return jsonify({"success": False, "error": "Failed to save config"}), 500
            
            return jsonify({"success": False, "error": f"Camera {camera_ip} not found"}), 404
            
    except Exception as e:
        logger.error(f"Error in camera config API for {camera_ip}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/cameras/bulk', methods=['POST'])
def bulk_update_cameras():
    """Массовое обновление камер (для импорта)"""
    try:
        data = request.json
        if not data or 'cameras' not in data:
            return jsonify({"success": False, "error": "No cameras data provided"}), 400
        
        # Обновляем список камер
        config['cameras'] = data['cameras']
        
        # Сохраняем
        if save_config():
            logger.info(f"✅ Bulk updated {len(data['cameras'])} cameras")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to save config"}), 500
            
    except Exception as e:
        logger.error(f"Error in bulk update: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/backup', methods=['POST'])
def create_config_backup():
    """Создать резервную копию конфигурации"""
    try:
        if os.path.exists(CONFIG_FILE):
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = f"{CONFIG_FILE}.{timestamp}.backup"
            shutil.copy2(CONFIG_FILE, backup_file)
            
            logger.info(f"✅ Config backup created: {backup_file}")
            return jsonify({
                "success": True,
                "backup": os.path.basename(backup_file)
            })
        else:
            return jsonify({"success": False, "error": "Config file not found"}), 404
            
    except Exception as e:
        logger.error(f"Error creating config backup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/restore/<backup_file>', methods=['POST'])
def restore_config_backup(backup_file):
    """Восстановить конфигурацию из резервной копии"""
    try:
        backup_path = os.path.join(os.path.dirname(CONFIG_FILE), backup_file)
        
        if not os.path.exists(backup_path):
            return jsonify({"success": False, "error": "Backup file not found"}), 404
        
        # Создаем бэкап текущей конфигурации перед восстановлением
        current_backup = f"{CONFIG_FILE}.before_restore.backup"
        shutil.copy2(CONFIG_FILE, current_backup)
        
        # Восстанавливаем
        shutil.copy2(backup_path, CONFIG_FILE)
        
        # Перезагружаем конфигурацию
        load_config()
        
        logger.info(f"✅ Config restored from {backup_file}")
        return jsonify({"success": True})
        
    except Exception as e:
        logger.error(f"Error restoring config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/backups', methods=['GET'])
def list_config_backups():
    """Получить список резервных копий конфигурации"""
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        backups = []
        
        for file in os.listdir(config_dir):
            if file.endswith('.backup'):
                filepath = os.path.join(config_dir, file)
                stat = os.stat(filepath)
                backups.append({
                    'name': file,
                    'size': stat.st_size,
                    'modified': stat.st_mtime * 1000,
                    'created': datetime.fromtimestamp(stat.st_ctime).isoformat()
                })
        
        # Сортируем по дате изменения (новые сверху)
        backups.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify({
            "success": True,
            "backups": backups
        })
        
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config/system', methods=['POST'])
def save_system_config():
    """Сохранить системные настройки (лимит записей и т.д.)"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
        
        # Сохраняем настройки в конфиг
        if 'maxRecordings' in data:
            # Здесь можно сохранить в config_manager
            # Пока сохраняем в отдельный файл или в localStorage на клиенте
            logger.info(f"System setting updated: maxRecordings = {data['maxRecordings']}")
            
            # Можно сохранить в config_manager.config
            if 'system' not in config_manager.config:
                config_manager.config['system'] = {}
            config_manager.config['system']['max_recordings'] = data['maxRecordings']
            config_manager.save_config()
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving system config: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



# ==================== БЛОК 6: ИНТЕГРАЦИЯ С HOME ASSISTANT ====================

@app.route('/api/ha/import_cameras', methods=['POST'])
def import_cameras_from_ha():
    """Импортировать камеры из интеграции OpenIPC в Home Assistant"""
    try:
        headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }
        
        url = f"{HASS_URL}/api/openipc/cameras"
        logger.info(f"📡 Requesting cameras from HA: {url}")
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"❌ Failed to get cameras from HA: HTTP {response.status_code}")
            return jsonify({
                "success": False, 
                "error": f"HTTP {response.status_code}"
            }), 500
        
        data = response.json()
        
        if not data.get('success'):
            return jsonify({"success": False, "error": "Invalid response from HA"}), 500
        
        ha_cameras = data.get('cameras', [])
        logger.info(f"📸 Found {len(ha_cameras)} cameras in HA")
        
        # Импортируем через менеджер
        result = config_manager.import_from_ha(ha_cameras)
        
        if config_manager.save_config():
            logger.info(f"✅ Imported {len(result['imported'])} new cameras, updated {len(result['updated'])} existing")
            return jsonify({
                "success": True,
                "imported": [c['name'] for c in result['imported']],
                "updated": [c['new_name'] for c in result['updated']],
                "total": result['total']
            })
        else:
            return jsonify({"success": False, "error": "Failed to save config"}), 500
        
    except Exception as e:
        logger.error(f"❌ Error importing cameras: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/ha/cameras', methods=['GET'])
def get_ha_cameras_list():
    """Получить список камер из HA без импорта"""
    try:
        headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }
        
        url = f"{HASS_URL}/api/openipc/cameras"
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code != 200:
            return jsonify({"success": False, "error": f"HTTP {response.status_code}"}), 500
        
        data = response.json()
        return jsonify(data)
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== БЛОК 7: HLS STREAMING API ====================
# (остается без изменений)

@app.route('/api/video/hls/<stream_name>.m3u8')
def serve_hls_playlist(stream_name):
    """Отдать HLS плейлист"""
    hls_dir = f"/tmp/hls/{stream_name}"
    playlist_path = f"{hls_dir}/playlist.m3u8"
    
    if os.path.exists(playlist_path):
        return send_from_directory(hls_dir, 'playlist.m3u8')
    return "Playlist not found", 404

@app.route('/api/video/hls/<stream_name>/<segment>')
def serve_hls_segment(stream_name, segment):
    """Отдать HLS сегмент"""
    return send_from_directory(f"/tmp/hls/{stream_name}", segment)

@app.route('/api/video/start_hls/<camera_ip>', methods=['POST'])
def start_hls_stream(camera_ip):
    """Запустить HLS трансляцию для камеры с использованием StreamManager"""
    data = request.json or {}
    stream_type = data.get('stream', 'main')
    
    cam_config = config_manager.get_camera(camera_ip)
    if not cam_config:
        return jsonify({"error": "Camera not found"}), 404
    
    output_name = f"{camera_ip}_{stream_type}"
    
    # Если монитор уже запущен, уведомляем об этом
    if hasattr(app, 'stream_monitor'):
        logger.info(f"Stream {output_name} will be monitored by StreamMonitor")
    
    # Останавливаем существующий менеджер если есть
    if output_name in stream_managers:
        logger.info(f"Stopping existing stream manager for {output_name}")
        stream_managers[output_name].stop()
        del stream_managers[output_name]
        time.sleep(1)
    
    # Создаем callback для обновления статуса
    def on_status_change(stream_name, status, restart_count):
        logger.info(f"Stream {stream_name} status: {status} (restarts: {restart_count})")
    
    # Создаем и запускаем новый менеджер
    manager = StreamManager(
        camera_ip=camera_ip,
        username=cam_config['username'],
        password=cam_config['password'],
        stream_type=stream_type,
        on_status_change=on_status_change
    )
    
    manager.start()
    stream_managers[output_name] = manager
    
    # Ждем немного для инициализации
    time.sleep(3)
    
    # Проверяем статус
    if manager.is_alive:
        # Проверяем создание плейлиста
        if os.path.exists(manager.playlist_path):
            file_size = os.path.getsize(manager.playlist_path)
            logger.info(f"✅ HLS playlist created for {output_name} ({file_size} bytes)")
            
            return jsonify({
                "success": True,
                "hls_url": manager.playlist_url,
                "stream": stream_type,
                "stats": manager.stats
            })
        else:
            logger.warning(f"⚠️ HLS playlist not yet created for {output_name}, but process is running")
            return jsonify({
                "success": True,
                "hls_url": manager.playlist_url,
                "stream": stream_type,
                "warning": "Playlist not yet created",
                "stats": manager.stats
            })
    else:
        error_details = f"Process died. Last error: {manager.last_error}"
        logger.error(f"❌ Failed to start HLS stream for {output_name}: {error_details}")
        
        # Читаем лог для диагностики
        log_tail = ""
        if os.path.exists(manager.log_file):
            with open(manager.log_file, 'r') as f:
                log_tail = f.read()[-1000:]
        
        return jsonify({
            "success": False,
            "error": "Failed to start HLS stream",
            "details": error_details,
            "log": log_tail,
            "stats": manager.stats
        }), 500

@app.route('/api/video/stop_hls/<camera_ip>', methods=['POST'])
def stop_hls_stream(camera_ip):
    """Остановить HLS трансляцию"""
    data = request.json or {}
    stream_type = data.get('stream', 'main')
    output_name = f"{camera_ip}_{stream_type}"
    
    if output_name in stream_managers:
        stream_managers[output_name].stop()
        del stream_managers[output_name]
        logger.info(f"✅ Stopped HLS stream for {output_name}")
        return jsonify({"success": True})
    
    return jsonify({"success": False, "error": "Stream not found"}), 404

@app.route('/api/video/hls_status/<camera_ip>')
def hls_status(camera_ip):
    """Статус HLS трансляций для камеры"""
    status = {
        "main": f"{camera_ip}_main" in stream_managers,
        "sub": f"{camera_ip}_sub" in stream_managers,
        "managers": {}
    }
    
    # Добавляем детальную статистику для каждого менеджера
    for name, manager in stream_managers.items():
        if camera_ip in name:
            status["managers"][name] = manager.stats
    
    return jsonify(status)

@app.route('/api/video/managers')
def list_stream_managers():
    """Список всех активных менеджеров потоков"""
    managers = []
    for name, manager in stream_managers.items():
        managers.append({
            "name": name,
            "stats": manager.stats
        })
    return jsonify({"success": True, "managers": managers})

@app.route('/api/video/restart_hls/<camera_ip>', methods=['POST'])
def restart_hls_stream(camera_ip):
    """Принудительно перезапустить HLS трансляцию"""
    data = request.json or {}
    stream_type = data.get('stream', 'main')
    output_name = f"{camera_ip}_{stream_type}"
    
    if output_name in stream_managers:
        stream_managers[output_name].restart()
        logger.info(f"🔄 Restarting HLS stream for {output_name}")
        return jsonify({"success": True})
    
    return jsonify({"success": False, "error": "Stream not found"}), 404


# ==================== БЛОК 8: RTSP ДИАГНОСТИКА ====================
# (остается без изменений)

@app.route('/api/video/diagnose_rtsp/<camera_ip>')
def diagnose_rtsp(camera_ip):
    """Подробная диагностика RTSP"""
    cam_config = config_manager.get_camera(camera_ip)
    if not cam_config:
        return jsonify({"error": "Camera not found"}), 404
    
    results = {
        "camera": camera_ip,
        "http_check": False,
        "rtsp_ports": {},
        "rtsp_streams": {}
    }
    
    # 1. Проверка HTTP доступности
    try:
        r = requests.get(f"http://{camera_ip}:80/", timeout=2)
        results['http_check'] = r.status_code == 200
    except:
        results['http_check'] = False
    
    # 2. Проверка разных портов RTSP
    for port in [554, 8554, 8555, 10554, 1935]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((camera_ip, port))
            results['rtsp_ports'][str(port)] = (result == 0)
            sock.close()
        except:
            results['rtsp_ports'][str(port)] = False
    
    # 3. Проверка разных путей RTSP
    paths = ['/stream=0', '/stream=1', '/live', '/av0_0', '/av0_1', '/', '/h264', '/ch0_0.h264']
    for path in paths:
        url = f"rtsp://{cam_config['username']}:{cam_config['password']}@{camera_ip}:554{path}"
        try:
            cmd = ['ffprobe', '-rtsp_transport', 'tcp', '-i', url, '-t', '1', '-show_streams', '-v', 'quiet']
            result = subprocess.run(cmd, capture_output=True, timeout=2)
            results['rtsp_streams'][path] = result.returncode == 0
        except:
            results['rtsp_streams'][path] = False
    
    return jsonify(results)

@app.route('/api/video/test/<camera_ip>')
def test_video_stream(camera_ip):
    """Тестовый эндпоинт для проверки стрима"""
    try:
        cam_config = config_manager.get_camera(camera_ip)
        if not cam_config:
            return jsonify({"error": "Camera not found"}), 404
            
        rtsp_url = f"rtsp://{cam_config['username']}:{cam_config['password']}@{camera_ip}:554/stream=0"
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            cmd = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-frames:v', '1',
                '-f', 'image2',
                '-y', tmp.name
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            
            if result.returncode == 0 and os.path.exists(tmp.name):
                with open(tmp.name, 'rb') as f:
                    img_data = f.read()
                os.unlink(tmp.name)
                return Response(img_data, mimetype='image/jpeg')
            else:
                error_msg = result.stderr.decode() if result.stderr else "Unknown error"
                return jsonify({
                    "error": "Failed to get snapshot",
                    "details": error_msg[:500]
                }), 500
                
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/camera/test_endpoints/<camera_ip>')
def test_camera_endpoints(camera_ip):
    """Тестирование всех возможных эндпоинтов для камеры"""
    cam_config = config_manager.get_camera(camera_ip)
    if not cam_config:
        return jsonify({"error": "Camera not found"}), 404
    
    username = cam_config.get('username', 'root')
    password = cam_config.get('password', '12345')
    auth = (username, password)
    
    all_endpoints = [
        "/image.jpg",
        "/snapshot.jpg",
        "/cgi-bin/snapshot.cgi",
        "/cgi-bin/images/0",
        "/cgi-bin/api.cgi?cmd=Snap&channel=0",
        "/cgi-bin/video.jpg",
        "/cgi-bin/jpg/image.cgi",
        "/cgi-bin/currenttime.cgi?cmd=snap",
        "/snapshot",
        "/img/snapshot.cgi",
        "/cgi-bin/encoder?USER=admin&PWD=&SNAPSHOT",
        "/cgi-bin/encoder?USER=admin&PWD=&SNAPSHOT&Quality=80",
        "/tmpfs/auto.jpg",
        "/onvif/snapshot"
    ]
    
    results = {}
    
    for endpoint in all_endpoints:
        url = f"http://{camera_ip}{endpoint}"
        try:
            start = time.time()
            response = requests.get(url, timeout=3, auth=auth)
            elapsed = time.time() - start
            
            if response.status_code == 200:
                data = response.content
                content_type = response.headers.get('Content-Type', '')
                results[endpoint] = {
                    "status": response.status_code,
                    "size": len(data),
                    "type": content_type,
                    "time": f"{elapsed:.2f}s",
                    "working": len(data) > 1000 and 'image' in content_type
                }
            else:
                results[endpoint] = {
                    "status": response.status_code,
                    "working": False
                }
        except requests.exceptions.Timeout:
            results[endpoint] = {
                "error": "timeout",
                "working": False
            }
        except Exception as e:
            results[endpoint] = {
                "error": str(e)[:50],
                "working": False
            }
    
    return jsonify({
        "camera": camera_ip,
        "results": results
    })


# ==================== БЛОК 9: QR-СКАНЕР, ГЕНЕРАТОР И TELEGRAM ====================
# (остается без изменений, только заменить config на config_manager.config)

# Статистика QR и Telegram
qr_stats = {
    "total_requests": 0,
    "successful_scans": 0,
    "failed_scans": 0,
    "total_codes_found": 0,
    "by_camera": {},
    "by_type": {},
    "last_scan_time": None,
    "last_code": None
}

# Хранилище истории Telegram
telegram_history = deque(maxlen=50)

# ===== TELEGRAM ЭНДПОИНТЫ =====

@app.route('/api/telegram/test', methods=['POST'])
def test_telegram():
    """Тест отправки в Telegram"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
            
        token = data.get('token')
        chat_id = data.get('chatId')
        
        # Если token не передан, пробуем взять из конфига
        if not token:
            token = config_manager.config.get('telegram', {}).get('bot_token')
            logger.info(f"Using token from config: {'***' if token else 'not found'}")
        
        # Если chat_id не передан, пробуем взять из конфига
        if not chat_id:
            chat_id = config_manager.config.get('telegram', {}).get('chat_id')
            logger.info(f"Using chat_id from config: {chat_id}")
        
        if not token:
            return jsonify({'success': False, 'error': 'Bot token not provided and not found in config'}), 400
            
        if not chat_id:
            return jsonify({'success': False, 'error': 'Chat ID not provided and not found in config'}), 400
        
        # Пробуем отправить тестовое сообщение
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            'chat_id': str(chat_id).strip(),
            'text': '✅ Тестовое сообщение от OpenIPC Bridge',
            'parse_mode': 'HTML'
        }
        
        logger.info(f"Sending test message to Telegram: chat_id={chat_id}")
        
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"Telegram response status: {response.status_code}")
        
        result = response.json()
        logger.info(f"Telegram response: {result}")
        
        success = result.get('ok', False)
        
        # Добавляем в историю
        telegram_history.appendleft({
            'time': datetime.now().strftime('%H:%M:%S'),
            'message': 'Тестовое сообщение',
            'status': 'success' if success else 'error',
            'details': result.get('description', '') if not success else '✓'
        })
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({
                'success': False, 
                'error': result.get('description', 'Unknown Telegram error')
            }), 400
            
    except requests.exceptions.Timeout:
        error_msg = "Timeout connecting to Telegram API"
        logger.error(f"❌ {error_msg}")
        telegram_history.appendleft({
            'time': datetime.now().strftime('%H:%M:%S'),
            'message': 'Тестовое сообщение',
            'status': 'error',
            'details': error_msg
        })
        return jsonify({'success': False, 'error': error_msg}), 500
        
    except requests.exceptions.ConnectionError:
        error_msg = "Connection error to Telegram API"
        logger.error(f"❌ {error_msg}")
        telegram_history.appendleft({
            'time': datetime.now().strftime('%H:%M:%S'),
            'message': 'Тестовое сообщение',
            'status': 'error',
            'details': error_msg
        })
        return jsonify({'success': False, 'error': error_msg}), 500
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Unexpected error in test_telegram: {error_msg}")
        logger.exception(e)
        telegram_history.appendleft({
            'time': datetime.now().strftime('%H:%M:%S'),
            'message': 'Тестовое сообщение',
            'status': 'error',
            'details': error_msg[:100]
        })
        return jsonify({'success': False, 'error': error_msg}), 500


@app.route('/api/telegram/history', methods=['GET'])
def get_telegram_history():
    """Получить историю отправки"""
    try:
        return jsonify({
            'success': True,
            'history': list(telegram_history)
        })
    except Exception as e:
        logger.error(f"Error getting telegram history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/telegram', methods=['POST'])
def save_telegram_config():
    """Сохранить настройки Telegram"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
        
        # Убеждаемся, что секция telegram существует
        if 'telegram' not in config_manager.config:
            config_manager.config['telegram'] = {}
        
        # Обновляем настройки
        config_manager.config['telegram'].update({
            'bot_token': data.get('bot_token', config_manager.config['telegram'].get('bot_token', '')),
            'chat_id': data.get('default_chat_id', config_manager.config['telegram'].get('chat_id', '')),
            'video_quality': data.get('video_quality', config_manager.config['telegram'].get('video_quality', 'high')),
            'max_size_mb': data.get('max_size_mb', config_manager.config['telegram'].get('max_size_mb', 50))
        })
        
        # Сохраняем конфигурацию
        if config_manager.save_config():
            logger.info(f"✅ Telegram config saved")
            
            # Добавляем в историю
            telegram_history.appendleft({
                'time': datetime.now().strftime('%H:%M:%S'),
                'message': 'Настройки Telegram обновлены',
                'status': 'success',
                'details': f"Token: {'***' if config_manager.config['telegram']['bot_token'] else 'not set'}, Chat ID: {config_manager.config['telegram']['chat_id']}"
            })
            
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to save config'}), 500
        
    except Exception as e:
        logger.error(f"❌ Failed to save telegram config: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/telegram/send_video', methods=['POST'])
def send_telegram_video():
    """Отправить видео в Telegram со сжатием"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data provided'}), 400
            
        video_path = data.get('video_path')
        caption = data.get('caption', '')
        chat_id = data.get('chat_id')
        quality = data.get('quality', config_manager.config.get('telegram', {}).get('video_quality', 'high'))
        
        if not video_path or not os.path.exists(video_path):
            return jsonify({'success': False, 'error': 'Video not found'}), 404
        
        bot_token = config_manager.config.get('telegram', {}).get('bot_token')
        if not bot_token:
            return jsonify({'success': False, 'error': 'Telegram bot not configured'}), 400
        
        # Определяем chat_id
        final_chat_id = chat_id or config_manager.config.get('telegram', {}).get('chat_id')
        if not final_chat_id:
            return jsonify({'success': False, 'error': 'No chat_id provided'}), 400
        
        # Проверяем размер файла
        file_size = os.path.getsize(video_path)
        max_size = config_manager.config.get('telegram', {}).get('max_size_mb', 50) * 1024 * 1024
        original_path = video_path
        compressed = False
        
        logger.info(f"Video size: {file_size/1024/1024:.1f} MB, max: {max_size/1024/1024:.1f} MB")
        
        # Если нужно сжатие
        if file_size > max_size or quality != 'original':
            logger.info(f"📦 Compressing video for Telegram: {video_path} (quality: {quality})")
            compressed_path = compress_video(video_path, quality)
            if compressed_path:
                video_path = compressed_path
                file_size = os.path.getsize(video_path)
                compressed = True
                logger.info(f"✅ Compressed: {file_size/1024/1024:.1f} MB")
            else:
                logger.warning("⚠️ Compression failed, sending original")
        
        # Отправляем
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
            
            with open(video_path, 'rb') as f:
                files = {'video': f}
                data = {
                    'chat_id': str(final_chat_id).strip(),
                    'caption': caption,
                    'supports_streaming': True
                }
                
                response = requests.post(url, files=files, data=data, timeout=120)
                result = response.json()
                success = result.get('ok', False)
                
                # Добавляем в историю
                telegram_history.appendleft({
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'message': f"Видео: {os.path.basename(original_path)}",
                    'status': 'success' if success else 'error',
                    'details': f"{file_size/1024/1024:.1f} MB, quality: {quality}" if success else result.get('description', '')
                })
                
                # Очищаем сжатый файл если он был создан
                if compressed and os.path.exists(video_path) and video_path != original_path:
                    try:
                        os.remove(video_path)
                        logger.debug(f"Removed temporary compressed file: {video_path}")
                    except Exception as e:
                        logger.error(f"Failed to remove temp file: {e}")
                
                if success:
                    return jsonify({'success': True})
                else:
                    return jsonify({
                        'success': False, 
                        'error': result.get('description', 'Unknown Telegram error')
                    }), 400
                    
        except Exception as e:
            logger.error(f"❌ Error sending video: {e}")
            telegram_history.appendleft({
                'time': datetime.now().strftime('%H:%M:%S'),
                'message': f"Видео: {os.path.basename(original_path)}",
                'status': 'error',
                'details': str(e)[:100]
            })
            return jsonify({'success': False, 'error': str(e)}), 500
            
    except Exception as e:
        logger.error(f"❌ Unexpected error in send_telegram_video: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


def compress_video(input_path, quality='medium'):
    """Сжать видео с помощью ffmpeg"""
    try:
        # Создаем имя для выходного файла
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_compressed_{quality}{ext}"
        
        # Параметры сжатия в зависимости от качества
        quality_params = {
            'high': {
                'scale': '1280:720',
                'video_bitrate': '1M',
                'audio_bitrate': '96k'
            },
            'medium': {
                'scale': '854:480',
                'video_bitrate': '500k',
                'audio_bitrate': '64k'
            },
            'low': {
                'scale': '640:360',
                'video_bitrate': '300k',
                'audio_bitrate': '48k'
            }
        }
        
        params = quality_params.get(quality, quality_params['medium'])
        
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-vf', f'scale={params["scale"]}',
            '-c:v', 'libx264',
            '-b:v', params['video_bitrate'],
            '-preset', 'fast',
            '-c:a', 'aac',
            '-b:a', params['audio_bitrate'],
            '-movflags', '+faststart',
            '-y',
            output_path
        ]
        
        logger.debug(f"Compress command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_path):
            output_size = os.path.getsize(output_path)
            input_size = os.path.getsize(input_path)
            ratio = (output_size / input_size) * 100
            logger.info(f"✅ Compression complete: {output_size/1024/1024:.1f} MB ({ratio:.1f}% of original)")
            return output_path
        else:
            logger.error(f"❌ Compression failed: {result.stderr}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error("❌ Compression timeout")
        return None
    except Exception as e:
        logger.error(f"❌ Compression error: {e}")
        return None


# ===== QR СТАТИСТИКА =====

@app.route('/api/debug/clear', methods=['POST'])
def clear_debug():
    """Очистить отладочные снимки"""
    try:
        debug_files = glob.glob("/config/www/qr_debug_*.jpg")
        marked_files = glob.glob("/config/www/qr_marked_*.jpg")
        
        for f in debug_files + marked_files:
            try:
                os.remove(f)
            except:
                pass
        
        logger.info(f"✅ Cleared {len(debug_files) + len(marked_files)} debug images")
        return jsonify({"success": True, "count": len(debug_files) + len(marked_files)})
    except Exception as e:
        logger.error(f"Error clearing debug: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/qr/stats', methods=['GET'])
def qr_statistics():
    """Получить статистику QR сканирования"""
    try:
        return jsonify({
            "success": True,
            "stats": qr_stats
        })
    except Exception as e:
        logger.error(f"Error getting QR stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/qr/debug', methods=['GET'])
def qr_debug():
    """Получить последние 50 строк отладки QR"""
    try:
        if os.path.exists(QR_DEBUG_FILE):
            with open(QR_DEBUG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()[-50:]
            return jsonify({
                "success": True,
                "debug": lines
            })
        return jsonify({"success": True, "debug": []})
    except Exception as e:
        logger.error(f"Error reading QR debug: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/send_telegram_photo', methods=['POST'])
def send_telegram_photo():
    """Отправить фото в Telegram"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400
            
        photo_base64 = data.get('photo')
        caption = data.get('caption', 'QR-код')
        chat_id = data.get('chat_id')
        
        if not photo_base64:
            return jsonify({"success": False, "error": "No photo data"}), 400
        
        if not chat_id:
            chat_id = config_manager.config.get('telegram', {}).get('chat_id')
        
        if not chat_id:
            return jsonify({"success": False, "error": "No chat_id configured"}), 400
        
        bot_token = config_manager.config.get('telegram', {}).get('bot_token')
        if not bot_token:
            return jsonify({"success": False, "error": "Telegram bot not configured"}), 400
        
        try:
            photo_bytes = base64.b64decode(photo_base64)
            
            url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            
            files = {
                'photo': ('qr.png', photo_bytes, 'image/png')
            }
            data = {
                'chat_id': str(chat_id).strip(),
                'caption': caption
            }
            
            response = requests.post(url, files=files, data=data, timeout=30)
            result = response.json()
            success = result.get('ok', False)
            
            # Добавляем в историю
            telegram_history.appendleft({
                'time': datetime.now().strftime('%H:%M:%S'),
                'message': f"Фото: {caption[:30]}...",
                'status': 'success' if success else 'error',
                'details': result.get('description', '') if not success else f"{len(photo_bytes)/1024:.1f} KB"
            })
            
            if success:
                logger.info(f"✅ Photo sent to Telegram chat {chat_id}")
                return jsonify({"success": True})
            else:
                logger.error(f"❌ Telegram error: {result}")
                return jsonify({"success": False, "error": result.get('description', 'Unknown error')}), 400
                
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            telegram_history.appendleft({
                'time': datetime.now().strftime('%H:%M:%S'),
                'message': f"Фото: {caption[:30]}...",
                'status': 'error',
                'details': str(e)[:100]
            })
            return jsonify({"success": False, "error": str(e)}), 500
            
    except Exception as e:
        logger.error(f"Unexpected error in send_telegram_photo: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С КАМЕРАМИ =====

def capture_snapshot_from_camera(camera_ip: str) -> Optional[bytes]:
    """Получить снимок с камеры используя конфигурацию"""
    global debug_counter
    
    try:
        cam_config = config_manager.get_camera(camera_ip)
        if not cam_config:
            logger.error(f"❌ No configuration found for camera {camera_ip}")
            return None
        
        camera_type = cam_config.get('type', 'openipc')
        username = cam_config.get('username', 'root')
        password = cam_config.get('password', '12345')
        
        # Специфичные эндпоинты для разных камер
        if camera_ip == "192.168.1.55":
            # Для этой камеры используем другие эндпоинты
            endpoints = [
                "/cgi-bin/snapshot.cgi",
                "/snapshot.jpg",
                "/cgi-bin/images/0",
                "/image.jpg",
                "/cgi-bin/api.cgi?cmd=Snap&channel=0",
                "/tmpfs/auto.jpg"
            ]
        else:
            endpoints = cam_config.get('snapshot_endpoints', [
                '/image.jpg', 
                '/cgi-bin/api.cgi?cmd=Snap&channel=0'
            ])
        
        auth = (username, password)
        
        for endpoint in endpoints:
            url = f"http://{camera_ip}{endpoint}"
            logger.info(f"📸 Capturing {camera_type} snapshot from {url}")
            
            try:
                response = requests.get(url, timeout=5, auth=auth)
                if response.status_code == 200:
                    data = response.content
                    if len(data) > 1000:
                        logger.info(f"✅ Snapshot captured: {len(data)} bytes from {endpoint}")
                        
                        # Сохраняем отладочный снимок для проблемных камер
                        if camera_ip == "192.168.1.55" and debug_counter % 10 == 0:
                            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                            debug_path = f"/config/www/snapshot_debug_{camera_ip}_{timestamp}.jpg"
                            try:
                                with open(debug_path, 'wb') as f:
                                    f.write(data)
                                logger.info(f"💾 Debug snapshot saved: {debug_path}")
                            except Exception as e:
                                logger.error(f"Failed to save debug snapshot: {e}")
                        
                        return data
                    else:
                        logger.warning(f"Snapshot too small: {len(data)} bytes from {endpoint}")
                else:
                    logger.warning(f"HTTP {response.status_code} from {endpoint}")
            except requests.exceptions.Timeout:
                logger.debug(f"Timeout connecting to {endpoint}")
                continue
            except Exception as e:
                logger.debug(f"Failed to connect to {endpoint}: {e}")
                continue
        
        logger.warning(f"❌ All snapshot attempts failed for {camera_ip}")
        return None
        
    except Exception as e:
        logger.error(f"Failed to capture snapshot: {e}")
        return None


def get_camera_entity_id(camera_ip: str) -> str:
    """Получить entity_id камеры по IP"""
    try:
        cam_config = config_manager.get_camera(camera_ip)
        if cam_config:
            name = cam_config.get('name', '').lower().replace(' ', '_')
            return f"camera.{name}"
        return f"camera.openipc_{camera_ip.replace('.', '_')}"
    except Exception as e:
        logger.error(f"Error getting camera entity_id: {e}")
        return f"camera.{camera_ip.replace('.', '_')}"




# ===== QR СКАНИРОВАНИЕ =====

def scan_qr_from_image(image_bytes: bytes) -> Optional[Dict]:
    """Сканировать QR код на изображении"""
    try:
        import cv2
        import numpy as np
        from pyzbar.pyzbar import decode
        
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            logger.error("Failed to decode image")
            return None
        
        height, width = img.shape[:2]
        logger.debug(f"Image size: {width}x{height}")
        
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        barcodes = decode(gray)
        
        if barcodes:
            barcode = barcodes[0]
            qr_data = barcode.data.decode('utf-8')
            qr_type = barcode.type
            
            logger.info(f"✅ QR Code found: {qr_data}")
            
            # Пытаемся сохранить отмеченное изображение
            try:
                points = barcode.polygon
                if len(points) == 4:
                    pts = np.array([(p.x, p.y) for p in points], np.int32)
                    pts = pts.reshape((-1, 1, 2))
                    cv2.polylines(img, [pts], True, (0, 255, 0), 3)
                    
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    marked_path = f"/config/www/qr_marked_{timestamp}.jpg"
                    cv2.imwrite(marked_path, img)
                    logger.info(f"💾 Marked image saved: {marked_path}")
            except Exception as e:
                logger.error(f"Failed to draw rectangle: {e}")
            
            return {
                "data": qr_data,
                "type": qr_type,
                "rect": {
                    "left": barcode.rect.left,
                    "top": barcode.rect.top,
                    "width": barcode.rect.width,
                    "height": barcode.rect.height
                }
            }
        else:
            logger.debug("No QR codes found in image")
            return None
            
    except ImportError as e:
        logger.error(f"QR scanning libraries not available: {e}")
        return None
    except Exception as e:
        logger.error(f"QR scan error: {e}")
        return None


def send_event_to_ha(event_type: str, event_data: dict):
    """Отправить событие в Home Assistant"""
    try:
        headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }
        
        url = f"{HASS_URL}/api/events/{event_type}"
        logger.info(f"📤 Sending event {event_type} to HA")
        
        response = requests.post(url, headers=headers, json=event_data, timeout=2)
        
        if response.status_code == 200:
            logger.info(f"✅ Event {event_type} sent to HA")
        else:
            logger.warning(f"Failed to send event: HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"Error sending event: {e}")


def continuous_scan(scan_id: str, camera_id: str, expected_code: str, timeout: int):
    """Непрерывное сканирование QR в фоновом потоке"""
    logger.info(f"🔄 Starting continuous scan {scan_id} for {camera_id}")
    
    start_time = time.time()
    scan_count = 0
    failed_attempts = 0
    
    while time.time() - start_time < timeout:
        scan_count += 1
        elapsed = int(time.time() - start_time)
        remaining = timeout - elapsed
        
        logger.info(f"📸 Scan #{scan_count} - {remaining}s remaining")
        
        try:
            snapshot = capture_snapshot_from_camera(camera_id)
            
            if snapshot:
                failed_attempts = 0
                qr_result = scan_qr_from_image(snapshot)
                
                if qr_result:
                    qr_data = qr_result.get('data', '')
                    
                    logger.info(f"🎯🎯🎯 QR CODE DETECTED: {qr_data}")
                    write_qr_debug(f"🎯 QR CODE DETECTED: {qr_data}")
                    
                    event_data = {
                        "camera": get_camera_entity_id(camera_id),
                        "data": qr_data,
                        "type": qr_result.get('type', 'QRCODE'),
                        "scan_id": scan_id,
                        "expected_code": expected_code,
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    send_event_to_ha("openipc_qr_detected", event_data)
                    
                    scan_jobs[scan_id].update({
                        "status": "completed",
                        "end_time": time.time(),
                        "result": qr_result,
                        "scan_count": scan_count
                    })
                    
                    logger.info(f"✅ Scan {scan_id} completed - code detected")
                    return
                else:
                    logger.debug("No QR code in this frame")
            else:
                failed_attempts += 1
                logger.warning(f"Failed to capture snapshot (attempt {failed_attempts})")
                
                if failed_attempts > 5:
                    logger.error(f"Camera {camera_id} seems unavailable - too many failed attempts")
                    break
            
            scan_jobs[scan_id].update({
                "scan_count": scan_count,
                "last_scan": time.time(),
                "status": "running"
            })
            
        except Exception as e:
            logger.error(f"Error in scan {scan_id}: {e}")
            write_qr_debug(f"❌ Error in scan: {e}")
        
        time.sleep(2)
    
    # Таймаут или ошибка
    camera_entity = get_camera_entity_id(camera_id)
    scan_jobs[scan_id].update({
        "status": "timeout" if time.time() - start_time >= timeout else "error",
        "end_time": time.time(),
        "result": None,
        "scan_count": scan_count
    })
    
    if time.time() - start_time >= timeout:
        event_data = {
            "camera": camera_entity,
            "scan_id": scan_id,
            "expected_code": expected_code,
            "timeout": timeout,
            "timestamp": datetime.now().isoformat()
        }
        send_event_to_ha("openipc_qr_timeout", event_data)
        logger.info(f"⏱️ Scan {scan_id} timed out after {timeout}s")
        write_qr_debug(f"⏱️ Scan timed out")
    else:
        logger.error(f"❌ Scan {scan_id} failed after {failed_attempts} failed attempts")
        write_qr_debug(f"❌ Scan failed - camera unavailable")


@app.route('/api/start_scan', methods=['POST'])
def start_scan():
    """Запуск непрерывного сканирования QR для камеры"""
    try:
        state["requests"] += 1
        data = request.json
        
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400
            
        camera_id = data.get('camera_id')
        expected_code = data.get('expected_code', 'a4625vol')
        timeout = data.get('timeout', 300)
        
        if not camera_id:
            return jsonify({"success": False, "error": "No camera_id provided"}), 400
        
        cam_config = get_camera_config(camera_id)
        if not cam_config:
            logger.warning(f"⚠️ No configuration found for camera {camera_id}, using defaults")
        
        logger.info(f"🎯 Starting continuous scan for {camera_id}")
        logger.info(f"🎯 Expected code: {expected_code}")
        write_qr_debug(f"🎯 Starting continuous scan for {camera_id} with expected code: {expected_code}")
        
        scan_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        scan_jobs[scan_id] = {
            "scan_id": scan_id,
            "camera_id": camera_id,
            "expected_code": expected_code,
            "timeout": timeout,
            "start_time": time.time(),
            "status": "starting",
            "result": None,
            "scan_count": 0
        }
        
        thread = threading.Thread(
            target=continuous_scan,
            args=(scan_id, camera_id, expected_code, timeout)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "success": True,
            "scan_id": scan_id,
            "message": f"Scan started for {camera_id}"
        })
        
    except Exception as e:
        logger.error(f"Error starting scan: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/scan_status/<scan_id>', methods=['GET'])
def scan_status(scan_id):
    """Получить статус сканирования"""
    try:
        state["requests"] += 1
        
        if scan_id in scan_jobs:
            return jsonify({
                "success": True,
                "scan": scan_jobs[scan_id]
            })
        
        return jsonify({
            "success": False,
            "error": "Scan not found"
        }), 404
        
    except Exception as e:
        logger.error(f"Error getting scan status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/stop_scan/<scan_id>', methods=['POST'])
def stop_scan(scan_id):
    """Остановить сканирование"""
    try:
        state["requests"] += 1
        
        if scan_id in scan_jobs:
            scan_jobs[scan_id].update({
                "status": "stopped",
                "end_time": time.time()
            })
            return jsonify({"success": True})
        
        return jsonify({"success": False, "error": "Scan not found"}), 404
        
    except Exception as e:
        logger.error(f"Error stopping scan: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/barcode', methods=['POST'])
def barcode():
    """Распознавание штрих-кода с диагностикой"""
    try:
        state["requests"] += 1
        qr_stats["total_requests"] += 1
        
        data = request.json
        
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400
        
        image_data = data.get('image', '')
        camera_id = data.get('camera_id', 'unknown')
        
        if not image_data:
            qr_stats["failed_scans"] += 1
            return jsonify({"success": False, "error": "No image data"}), 400
        
        start_time = time.time()
        
        try:
            import cv2
            import numpy as np
            from pyzbar.pyzbar import decode
            
            try:
                img_bytes = base64.b64decode(image_data)
                logger.info(f"Decoded {len(img_bytes)} bytes")
            except Exception as e:
                qr_stats["failed_scans"] += 1
                return jsonify({"success": False, "error": f"Base64 decode failed: {e}"}), 400
            
            nparr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is None:
                qr_stats["failed_scans"] += 1
                return jsonify({"success": False, "error": "Failed to decode image"}), 400
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            barcodes = decode(gray)
            
            process_time = time.time() - start_time
            
            if barcodes:
                qr_stats["successful_scans"] += 1
                qr_stats["last_scan_time"] = datetime.now().isoformat()
                
                results = []
                for barcode in barcodes:
                    barcode_data = barcode.data.decode('utf-8')
                    barcode_type = barcode.type
                    
                    qr_stats["total_codes_found"] += 1
                    qr_stats["by_type"][barcode_type] = qr_stats["by_type"].get(barcode_type, 0) + 1
                    
                    if camera_id not in qr_stats["by_camera"]:
                        qr_stats["by_camera"][camera_id] = {"scans": 0, "codes": 0}
                    qr_stats["by_camera"][camera_id]["codes"] += 1
                    
                    qr_stats["last_code"] = barcode_data
                    
                    results.append({
                        "data": barcode_data,
                        "type": barcode_type,
                        "rect": {
                            "left": barcode.rect.left,
                            "top": barcode.rect.top,
                            "width": barcode.rect.width,
                            "height": barcode.rect.height
                        }
                    })
                
                if camera_id not in qr_stats["by_camera"]:
                    qr_stats["by_camera"][camera_id] = {"scans": 0, "codes": 0}
                qr_stats["by_camera"][camera_id]["scans"] += 1
                
                logger.info(f"✅ Found {len(results)} barcodes in {process_time:.2f}s")
                
                return jsonify({
                    "success": True,
                    "barcodes": results,
                    "stats": {
                        "process_time_ms": int(process_time * 1000),
                        "codes_found": len(results)
                    }
                })
            else:
                qr_stats["failed_scans"] += 1
                
                if camera_id not in qr_stats["by_camera"]:
                    qr_stats["by_camera"][camera_id] = {"scans": 0, "codes": 0}
                qr_stats["by_camera"][camera_id]["scans"] += 1
                
                logger.debug(f"No barcodes found in {process_time:.2f}s")
                
                return jsonify({
                    "success": True,
                    "barcodes": [],
                    "stats": {
                        "process_time_ms": int(process_time * 1000),
                        "codes_found": 0
                    }
                })
            
        except ImportError as e:
            qr_stats["failed_scans"] += 1
            logger.error(f"QR libraries not available: {e}")
            return jsonify({"success": False, "error": "QR scanning libraries not installed"}), 500
        except Exception as e:
            qr_stats["failed_scans"] += 1
            logger.error(f"Barcode error: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
            
    except Exception as e:
        logger.error(f"Unexpected error in barcode: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/camera/<path:camera_id>/barcode', methods=['POST'])
def camera_barcode(camera_id):
    """Endpoint для QR сканирования конкретной камеры"""
    try:
        logger.info(f"Camera barcode endpoint called for: {camera_id}")
        data = request.json or {}
        data['camera_id'] = camera_id
        return barcode()
    except Exception as e:
        logger.error(f"Error in camera_barcode: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== БЛОК 10: TTS API ====================

@app.route('/api/tts', methods=['POST'])
def tts():
    state["requests"] += 1
    data = request.json
    
    camera_id = data.get('camera_id')
    text = data.get('text', '')
    lang = data.get('lang', 'ru')
    provider = data.get('provider', config['tts']['provider'])
    
    logger.info(f"TTS request: camera={camera_id}, text={text}, provider={provider}")
    
    if not camera_id or not text:
        return jsonify({"success": False, "error": "Missing camera_id or text"}), 400
    
    # Получаем конфигурацию камеры
    cam_config = get_camera_config(camera_id)
    if not cam_config:
        cam_config = get_camera_config_by_name(camera_id)
    
    if cam_config:
        camera_ip = cam_config['ip']
        camera_type = cam_config['type']
        username = cam_config['username']
        password = cam_config['password']
        tts_format = cam_config.get('tts_format', 'pcm' if camera_type == 'openipc' else 'alaw')
        tts_endpoint = cam_config.get('tts_endpoint', '/cgi-bin/audio/transmit.cgi' if camera_type == 'beward' else '/play_audio')
    else:
        # Fallback на старую логику
        if camera_id == '192.168.1.10' or 'beward' in str(camera_id).lower():
            camera_type = 'beward'
            username = 'admin'
            password = 'Q96811621w'
            camera_ip = '192.168.1.10'
            tts_format = 'alaw'
            tts_endpoint = '/cgi-bin/audio/transmit.cgi'
        else:
            camera_type = 'openipc'
            username = 'root'
            password = '12345'
            ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', camera_id)
            camera_ip = ip_match.group(1) if ip_match else '192.168.1.4'
            tts_format = 'pcm'
            tts_endpoint = '/play_audio'
    
    camera_data = {
        "ip": camera_ip,
        "type": camera_type,
        "username": username,
        "password": password,
        "format": tts_format,
        "endpoint": tts_endpoint
    }
    
    if camera_type == 'beward' or tts_format == 'alaw':
        return _tts_for_beward(camera_data, text, lang)
    else:
        return _tts_for_openipc(camera_data, text, lang, provider)


@app.route('/api/camera/<path:camera_id>/tts', methods=['POST'])
def camera_tts(camera_id):
    logger.info(f"Camera TTS endpoint called for: {camera_id}")
    data = request.json or {}
    data['camera_id'] = camera_id
    return tts()


def _tts_for_beward(camera, text, lang):
    logger.info(f"Beward TTS to {camera['ip']}")
    
    with tempfile.NamedTemporaryFile(suffix='.alaw', delete=False) as tmp:
        alaw_path = tmp.name
    
    try:
        if not os.path.exists(TTS_GENERATE_BEWARD_SCRIPT):
            logger.error(f"Beward script not found: {TTS_GENERATE_BEWARD_SCRIPT}")
            return jsonify({"success": False, "error": "Beward script not found"}), 500
        
        # Генерируем A-law файл
        cmd = ["bash", TTS_GENERATE_BEWARD_SCRIPT, text, lang, alaw_path]
        logger.debug(f"Running command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logger.error(f"TTS generation failed: {result.stderr}")
            return jsonify({"success": False, "error": "TTS generation failed"}), 500
            
        if not os.path.exists(alaw_path):
            logger.error("TTS file not created")
            return jsonify({"success": False, "error": "TTS file not created"}), 500
        
        # Читаем аудио файл
        with open(alaw_path, 'rb') as f:
            audio_data = f.read()
        
        logger.info(f"Generated {len(audio_data)} bytes of A-law audio")
        
        # Сохраняем копию для отладки
        if debug_counter % 5 == 0:
            debug_audio_path = f"/config/www/tts_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.alaw"
            try:
                shutil.copy2(alaw_path, debug_audio_path)
                logger.info(f"💾 Debug audio saved to {debug_audio_path}")
            except Exception as e:
                logger.error(f"Failed to save debug audio: {e}")
        
        # Формируем правильные заголовки как в документации Beward
        endpoint = camera.get('endpoint', '/cgi-bin/audio/transmit.cgi')
        url = f"http://{camera['ip']}{endpoint}"
        
        # Создаем базовую аутентификацию
        auth_str = base64.b64encode(f"{camera['username']}:{camera['password']}".encode()).decode()
        
        headers = {
            "Content-Type": "audio/basic",
            "Content-Length": str(len(audio_data)),
            "Connection": "Keep-Alive",
            "Cache-Control": "no-cache",
            "Authorization": f"Basic {auth_str}"
        }
        
        logger.debug(f"Sending to {url}")
        logger.debug(f"Headers: {headers}")
        
        # Отправляем POST запрос
        response = requests.post(url, headers=headers, data=audio_data, timeout=10)
        
        logger.info(f"Response status: {response.status_code}")
        logger.debug(f"Response headers: {response.headers}")
        
        if response.status_code == 200:
            logger.info(f"✅ TTS sent successfully to Beward")
            return jsonify({"success": True})
        else:
            logger.error(f"❌ TTS failed: HTTP {response.status_code}")
            try:
                error_text = response.text
                logger.error(f"Response body: {error_text[:200]}")
            except:
                pass
            return jsonify({"success": False, "error": f"HTTP {response.status_code}"}), 500
            
    except subprocess.TimeoutExpired:
        logger.error("TTS generation timeout")
        return jsonify({"success": False, "error": "TTS generation timeout"}), 500
    except Exception as e:
        logger.error(f"TTS error: {e}")
        logger.exception(e)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        # Очищаем временный файл
        if os.path.exists(alaw_path):
            try:
                os.unlink(alaw_path)
            except:
                pass


def _tts_for_openipc(camera, text, lang, provider='google'):
    logger.info(f"OpenIPC TTS to {camera['ip']} with provider {provider}")
    
    with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as tmp:
        pcm_path = tmp.name
    
    try:
        # Выбираем провайдера
        if provider == 'rhvoice':
            if not os.path.exists(TTS_GENERATE_RHVoice_SCRIPT):
                return jsonify({"success": False, "error": "RHVoice script not found"}), 500
            cmd = ["bash", TTS_GENERATE_RHVoice_SCRIPT, text, lang, pcm_path]
        
        elif provider == 'yandex':
            # Для Yandex нужно будет добавить отдельный скрипт
            return jsonify({"success": False, "error": "Yandex TTS not implemented yet"}), 500
        
        else:  # google по умолчанию
            if not os.path.exists(TTS_GENERATE_SCRIPT):
                return jsonify({"success": False, "error": "Google TTS script not found"}), 500
            cmd = ["bash", TTS_GENERATE_SCRIPT, text, lang, pcm_path]
        
        logger.debug(f"Running command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logger.error(f"TTS generation failed: {result.stderr}")
            return jsonify({"success": False, "error": "TTS generation failed"}), 500
            
        if not os.path.exists(pcm_path):
            logger.error("TTS file not created")
            return jsonify({"success": False, "error": "TTS file not created"}), 500
        
        with open(pcm_path, 'rb') as f:
            audio_data = f.read()
        
        logger.info(f"Generated {len(audio_data)} bytes of PCM audio")
        
        # Сохраняем копию для отладки
        debug_audio_path = f"/config/www/tts_debug_{provider}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pcm"
        try:
            shutil.copy2(pcm_path, debug_audio_path)
            logger.info(f"💾 Debug audio saved to {debug_audio_path}")
        except Exception as e:
            logger.error(f"Failed to save debug audio: {e}")
        
        endpoint = camera.get('endpoint', '/play_audio')
        url = f"http://{camera['ip']}{endpoint}"
        auth = (camera['username'], camera['password'])
        
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio_data))
        }
        
        response = requests.post(url, headers=headers, data=audio_data, auth=auth, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✅ TTS sent successfully to OpenIPC")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": f"HTTP {response.status_code}"}), 500
            
    finally:
        if os.path.exists(pcm_path):
            os.unlink(pcm_path)


# ==================== БЛОК 11: OSD API ====================

@app.route('/api/osd/cameras', methods=['GET'])
def list_osd_cameras():
    """Список камер с поддержкой OSD"""
    cameras = []
    for cam in config['cameras']:
        if 'osd' in cam:
            cameras.append({
                "ip": cam['ip'],
                "name": cam['name'],
                "type": cam['type'],
                "osd_enabled": cam['osd'].get('enabled', False),
                "osd_port": cam['osd'].get('port', 9000)
            })
        else:
            cameras.append({
                "ip": cam['ip'],
                "name": cam['name'],
                "type": cam['type'],
                "osd_enabled": False,
                "osd_port": 9000
            })
    return jsonify({"success": True, "cameras": cameras})

@app.route('/api/osd/camera/<path:camera_ip>', methods=['GET'])
def get_camera_osd_config(camera_ip):
    """Получить конфигурацию OSD для камеры"""
    cam_config = get_camera_config(camera_ip)
    if not cam_config:
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    if 'osd' not in cam_config:
        cam_config['osd'] = {
            "enabled": True,
            "port": 9000,
            "time_format": "%d.%m.%Y %H:%M:%S",
            "regions": {}
        }
    
    try:
        osd_port = cam_config['osd'].get('port', 9000)
        auth = (cam_config['username'], cam_config['password'])
        
        for region in range(4):
            url = f"http://{camera_ip}:{osd_port}/api/osd/{region}"
            try:
                response = requests.get(url, auth=auth, timeout=2)
                if response.status_code == 200:
                    cam_config['osd']['regions'][str(region)] = response.json()
            except Exception as e:
                logger.debug(f"Failed to get OSD state for region {region}: {e}")
    except Exception as e:
        logger.error(f"Failed to get OSD state: {e}")
    
    return jsonify({"success": True, "config": cam_config['osd']})

@app.route('/api/osd/camera/<path:camera_ip>/region/<int:region>', methods=['POST'])
def set_osd_region(camera_ip, region):
    """Установить параметры региона OSD"""
    data = request.json
    
    cam_config = get_camera_config(camera_ip)
    if not cam_config:
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    osd_port = cam_config.get('osd', {}).get('port', 9000)
    auth = (cam_config['username'], cam_config['password'])
    
    params = []
    
    if 'text' in data:
        text = data['text']
        if text == "":
            params.append("text=")
        else:
            params.append(f"text={requests.utils.quote(text)}")
    
    if 'color' in data:
        color = data['color'].replace('#', '')
        params.append(f"color=%23{color}")
    
    if 'size' in data:
        params.append(f"size={data['size']}")
    
    if 'posx' in data:
        params.append(f"posx={data['posx']}")
    
    if 'posy' in data:
        params.append(f"posy={data['posy']}")
    
    if 'opacity' in data:
        params.append(f"opacity={data['opacity']}")
    
    if 'font' in data:
        params.append(f"font={data['font']}")
    
    if 'outline' in data:
        outline = data['outline'].replace('#', '')
        params.append(f"outl=%23{outline}")
    
    if 'thickness' in data:
        params.append(f"thick={data['thickness']}")
    
    if not params:
        return jsonify({"success": False, "error": "No parameters"}), 400
    
    url = f"http://{camera_ip}:{osd_port}/api/osd/{region}?{'&'.join(params)}"
    logger.info(f"Setting OSD region {region}: {url}")
    
    try:
        response = requests.get(url, auth=auth, timeout=5)
        return jsonify({
            "success": response.status_code == 200,
            "status": response.status_code,
            "response": response.text
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/osd/camera/<path:camera_ip>/region/<int:region>/clear', methods=['POST'])
def clear_osd_region(camera_ip, region):
    """Очистить регион OSD"""
    cam_config = get_camera_config(camera_ip)
    if not cam_config:
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    osd_port = cam_config.get('osd', {}).get('port', 9000)
    auth = (cam_config['username'], cam_config['password'])
    
    url = f"http://{camera_ip}:{osd_port}/api/osd/{region}?text="
    
    try:
        response = requests.get(url, auth=auth, timeout=5)
        return jsonify({
            "success": response.status_code == 200,
            "status": response.status_code
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/osd/camera/<path:camera_ip>/time', methods=['POST'])
def set_osd_time_format(camera_ip):
    """Установить формат времени для $t"""
    data = request.json
    time_format = data.get('format', '%d.%m.%Y %H:%M:%S')
    
    cam_config = get_camera_config(camera_ip)
    if not cam_config:
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    osd_port = cam_config.get('osd', {}).get('port', 9000)
    auth = (cam_config['username'], cam_config['password'])
    
    escaped_format = time_format.replace('%', '%25')
    url = f"http://{camera_ip}:{osd_port}/api/time?fmt={escaped_format}"
    
    try:
        response = requests.get(url, auth=auth, timeout=5)
        return jsonify({
            "success": response.status_code == 200,
            "status": response.status_code,
            "format": time_format
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/osd/camera/<path:camera_ip>/logo', methods=['POST'])
def upload_osd_logo(camera_ip):
    """Загрузить логотип в OSD"""
    data = request.json
    region = data.get('region', 0)
    logo_path = data.get('logo_path')
    posx = data.get('posx', 10)
    posy = data.get('posy', 10)
    
    if not logo_path or not os.path.exists(logo_path):
        return jsonify({"success": False, "error": "Logo file not found"}), 404
    
    cam_config = get_camera_config(camera_ip)
    if not cam_config:
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    osd_port = cam_config.get('osd', {}).get('port', 9000)
    auth = (cam_config['username'], cam_config['password'])
    
    url = f"http://{camera_ip}:{osd_port}/api/osd/{region}?posx={posx}&posy={posy}"
    
    try:
        with open(logo_path, 'rb') as f:
            files = {'data': f}
            response = requests.post(url, files=files, auth=auth, timeout=10)
        
        return jsonify({
            "success": response.status_code == 200,
            "status": response.status_code
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== БЛОК 12: ФАЙЛОВЫЙ МЕНЕДЖЕР API ====================

@app.route('/api/files/list')
def list_files():
    """Получить список доступных файлов"""
    try:
        files = []
        for filename in os.listdir(WORKING_DIR):
            filepath = os.path.join(WORKING_DIR, filename)
            if os.path.isfile(filepath):
                ext = os.path.splitext(filename)[1]
                if ext in ALLOWED_EXTENSIONS or filename == 'server.py':
                    stat = os.stat(filepath)
                    files.append({
                        "name": filename,
                        "size": stat.st_size,
                        "modified": stat.st_mtime * 1000,  # в миллисекундах для JS
                        "ext": ext
                    })
        
        # Сортируем по имени
        files.sort(key=lambda x: x['name'])
        
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/files/get/<path:filename>')
def get_file(filename):
    """Получить содержимое файла"""
    try:
        # Защита от directory traversal
        if '..' in filename or filename.startswith('/'):
            return jsonify({"success": False, "error": "Invalid filename"}), 400
        
        filepath = os.path.join(WORKING_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({"success": False, "error": "File not found"}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return jsonify({"success": True, "content": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/files/save', methods=['POST'])
def save_file():
    """Сохранить содержимое файла"""
    try:
        data = request.json
        filename = data.get('filename')
        content = data.get('content')
        
        if not filename or content is None:
            return jsonify({"success": False, "error": "Missing filename or content"}), 400
        
        # Защита от directory traversal
        if '..' in filename or filename.startswith('/'):
            return jsonify({"success": False, "error": "Invalid filename"}), 400
        
        filepath = os.path.join(WORKING_DIR, filename)
        
        # Создаем бэкап автоматически
        if os.path.exists(filepath):
            backup_dir = os.path.join(WORKING_DIR, 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"{filename}.{timestamp}.backup"
            backup_path = os.path.join(backup_dir, backup_name)
            
            shutil.copy2(filepath, backup_path)
            logger.info(f"✅ Created backup: {backup_name}")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"✅ Saved file: {filename}")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving file: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/files/upload', methods=['POST'])
def upload_file():
    """Загрузить новый файл"""
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "Empty filename"}), 400
        
        filename = file.filename
        
        # Проверка расширения
        ext = os.path.splitext(filename)[1]
        if ext not in ALLOWED_EXTENSIONS and filename != 'server.py':
            return jsonify({"success": False, "error": f"File type {ext} not allowed"}), 400
        
        # Защита от directory traversal
        if '..' in filename or filename.startswith('/'):
            return jsonify({"success": False, "error": "Invalid filename"}), 400
        
        filepath = os.path.join(WORKING_DIR, filename)
        
        # Создаем бэкап если файл существует
        if os.path.exists(filepath):
            backup_dir = os.path.join(WORKING_DIR, 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"{filename}.{timestamp}.backup"
            backup_path = os.path.join(backup_dir, backup_name)
            
            shutil.copy2(filepath, backup_path)
            logger.info(f"✅ Created backup before upload: {backup_name}")
        
        file.save(filepath)
        logger.info(f"✅ Uploaded file: {filename}")
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/files/download/<path:filename>')
def download_file(filename):
    """Скачать файл"""
    try:
        # Защита от directory traversal
        if '..' in filename or filename.startswith('/'):
            return "Invalid filename", 400
        
        filepath = os.path.join(WORKING_DIR, filename)
        if not os.path.exists(filepath):
            return "File not found", 404
        
        return send_from_directory(WORKING_DIR, filename, as_attachment=True)
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return str(e), 500

@app.route('/api/files/backup/<path:filename>', methods=['POST'])
def backup_file(filename):
    """Создать резервную копию файла"""
    try:
        # Защита от directory traversal
        if '..' in filename or filename.startswith('/'):
            return jsonify({"success": False, "error": "Invalid filename"}), 400
        
        filepath = os.path.join(WORKING_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({"success": False, "error": "File not found"}), 404
        
        backup_dir = os.path.join(WORKING_DIR, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{filename}.{timestamp}.backup"
        backup_path = os.path.join(backup_dir, backup_name)
        
        shutil.copy2(filepath, backup_path)
        logger.info(f"✅ Created backup: {backup_name}")
        
        return jsonify({"success": True, "backup": backup_name})
    except Exception as e:
        logger.error(f"Error creating backup: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/translations/<lang>')
def get_translations(lang):
    """Получить переводы для указанного языка"""
    return jsonify(load_translations(lang))





# ==================== БЛОК 14: API ДЛЯ СНИМКОВ ====================

@app.route('/api/snapshots/list', methods=['GET'])
def list_snapshots():
    """Получить список всех снимков"""
    try:
        snapshots = []
        snapshots_dir = '/config/www/snapshots'
        
        logger.info(f"Scanning for snapshots in {snapshots_dir}")
        
        if not os.path.exists(snapshots_dir):
            logger.warning(f"Snapshots directory not found: {snapshots_dir}")
            return jsonify({'success': True, 'snapshots': []})
        
        for root, dirs, files in os.walk(snapshots_dir):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    filepath = os.path.join(root, file)
                    stat = os.stat(filepath)
                    
                    # Извлекаем информацию из пути
                    rel_path = filepath.replace('/config/www/', '')
                    url = f"/{rel_path}"
                    
                    # Парсим путь для получения информации
                    # Пример пути: /config/www/snapshots/2026-03-16/192_168_1_4/manual/15-30-22.jpg
                    path_parts = root.split('/')
                    date = 'unknown'
                    camera = 'unknown'
                    snap_type = 'unknown'
                    
                    if len(path_parts) >= 5:
                        date = path_parts[-3]
                        camera = path_parts[-2]
                        snap_type = path_parts[-1]
                    
                    # Получаем имя камеры из конфига если возможно
                    camera_name = camera
                    try:
                        # Преобразуем 192_168_1_4 обратно в IP 192.168.1.4
                        camera_ip = camera.replace('_', '.')
                        cam_config = get_camera_config(camera_ip)
                        if cam_config:
                            camera_name = cam_config.get('name', camera)
                    except Exception as e:
                        logger.debug(f"Failed to get camera name for {camera}: {e}")
                    
                    snapshots.append({
                        'id': hashlib.md5(filepath.encode()).hexdigest()[:8],
                        'path': url,
                        'filename': file,
                        'camera': camera.replace('_', '.'),  # Преобразуем обратно в IP
                        'camera_name': camera_name,
                        'date': date,
                        'type': snap_type,
                        'size': stat.st_size,
                        'time': stat.st_ctime,
                        'width': 0,  # Можно добавить определение размера изображения позже
                        'height': 0
                    })
                    
                    logger.debug(f"Found snapshot: {filepath} - {snap_type} - {camera}")
        
        # Сортируем по времени (новые сверху)
        snapshots.sort(key=lambda x: x['time'], reverse=True)
        
        logger.info(f"✅ Found {len(snapshots)} snapshots")
        return jsonify({'success': True, 'snapshots': snapshots})
        
    except Exception as e:
        logger.error(f"❌ Error listing snapshots: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/snapshots/delete', methods=['POST'])
def delete_snapshot():
    """Удалить снимок"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        path = data.get('path')
        
        if not path:
            return jsonify({'success': False, 'error': 'No path provided'}), 400
        
        # Преобразуем URL в путь к файлу
        # URL вида: /snapshots/2026-03-16/192_168_1_4/manual/15-30-22.jpg
        # Преобразуем в: /config/www/snapshots/2026-03-16/192_168_1_4/manual/15-30-22.jpg
        if path.startswith('/snapshots/'):
            filepath = path.replace('/snapshots/', '/config/www/snapshots/')
        else:
            filepath = path.replace('/config/www/', '')
            filepath = os.path.join('/config/www', filepath)
        
        logger.info(f"Attempting to delete snapshot: {filepath}")
        
        if not os.path.exists(filepath):
            logger.warning(f"Snapshot file not found: {filepath}")
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        # Проверяем, что файл находится в директории snapshots (безопасность)
        if not filepath.startswith('/config/www/snapshots/'):
            logger.error(f"Security violation: Attempt to delete file outside snapshots directory: {filepath}")
            return jsonify({'success': False, 'error': 'Invalid file path'}), 403
        
        os.remove(filepath)
        logger.info(f"✅ Deleted snapshot: {filepath}")
        
        # Также удаляем thumbnail если есть
        thumb_path = filepath.replace('.jpg', '_thumb.jpg').replace('.png', '_thumb.png')
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
            logger.debug(f"Deleted thumbnail: {thumb_path}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"❌ Error deleting snapshot: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/snapshots/info', methods=['POST'])
def get_snapshot_info():
    """Получить детальную информацию о снимке"""
    try:
        data = request.json
        path = data.get('path')
        
        if not path:
            return jsonify({'success': False, 'error': 'No path provided'}), 400
        
        # Преобразуем URL в путь к файлу
        if path.startswith('/snapshots/'):
            filepath = path.replace('/snapshots/', '/config/www/snapshots/')
        else:
            filepath = path
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        
        stat = os.stat(filepath)
        
        # Пытаемся получить размеры изображения
        width = 0
        height = 0
        try:
            from PIL import Image
            img = Image.open(filepath)
            width, height = img.size
        except ImportError:
            logger.debug("PIL not available, skipping image size detection")
        except Exception as e:
            logger.debug(f"Failed to get image size: {e}")
        
        # Получаем информацию из пути
        path_parts = filepath.split('/')
        date = 'unknown'
        camera = 'unknown'
        snap_type = 'unknown'
        
        if len(path_parts) >= 6:
            date = path_parts[-4]
            camera = path_parts[-3]
            snap_type = path_parts[-2]
        
        return jsonify({
            'success': True,
            'info': {
                'size': stat.st_size,
                'created': stat.st_ctime,
                'modified': stat.st_mtime,
                'width': width,
                'height': height,
                'date': date,
                'camera': camera,
                'type': snap_type,
                'filename': os.path.basename(filepath)
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting snapshot info: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/snapshots/cleanup', methods=['POST'])
def cleanup_old_snapshots():
    """Удалить старые снимки (старше N дней)"""
    try:
        data = request.json
        days = data.get('days', 30)  # По умолчанию 30 дней
        
        cutoff_time = time.time() - (days * 24 * 3600)
        snapshots_dir = '/config/www/snapshots'
        deleted_count = 0
        freed_space = 0
        
        logger.info(f"🧹 Cleaning up snapshots older than {days} days")
        
        for root, dirs, files in os.walk(snapshots_dir):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    filepath = os.path.join(root, file)
                    stat = os.stat(filepath)
                    
                    if stat.st_ctime < cutoff_time:
                        file_size = stat.st_size
                        os.remove(filepath)
                        deleted_count += 1
                        freed_space += file_size
                        logger.debug(f"Deleted old snapshot: {filepath}")
                        
                        # Удаляем thumbnail если есть
                        thumb_path = filepath.replace('.jpg', '_thumb.jpg').replace('.png', '_thumb.png')
                        if os.path.exists(thumb_path):
                            os.remove(thumb_path)
        
        logger.info(f"✅ Cleaned up {deleted_count} old snapshots, freed {freed_space / (1024*1024):.1f} MB")
        return jsonify({
            'success': True,
            'deleted': deleted_count,
            'freed_space_mb': freed_space / (1024 * 1024)
        })
        
    except Exception as e:
        logger.error(f"Error cleaning up snapshots: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/snapshots/stats', methods=['GET'])
def get_snapshots_stats():
    """Получить статистику по снимкам"""
    try:
        snapshots_dir = '/config/www/snapshots'
        total_size = 0
        total_count = 0
        by_date = {}
        by_camera = {}
        by_type = {}
        
        if not os.path.exists(snapshots_dir):
            return jsonify({
                'success': True,
                'stats': {
                    'total_count': 0,
                    'total_size_mb': 0,
                    'by_date': {},
                    'by_camera': {},
                    'by_type': {}
                }
            })
        
        for root, dirs, files in os.walk(snapshots_dir):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    filepath = os.path.join(root, file)
                    stat = os.stat(filepath)
                    
                    total_count += 1
                    total_size += stat.st_size
                    
                    # Парсим путь
                    path_parts = root.split('/')
                    if len(path_parts) >= 5:
                        date = path_parts[-3]
                        camera = path_parts[-2]
                        snap_type = path_parts[-1]
                        
                        # По дате
                        if date not in by_date:
                            by_date[date] = {'count': 0, 'size': 0}
                        by_date[date]['count'] += 1
                        by_date[date]['size'] += stat.st_size
                        
                        # По камере
                        if camera not in by_camera:
                            by_camera[camera] = {'count': 0, 'size': 0}
                        by_camera[camera]['count'] += 1
                        by_camera[camera]['size'] += stat.st_size
                        
                        # По типу
                        if snap_type not in by_type:
                            by_type[snap_type] = {'count': 0, 'size': 0}
                        by_type[snap_type]['count'] += 1
                        by_type[snap_type]['size'] += stat.st_size
        
        return jsonify({
            'success': True,
            'stats': {
                'total_count': total_count,
                'total_size_mb': total_size / (1024 * 1024),
                'by_date': by_date,
                'by_camera': by_camera,
                'by_type': by_type
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting snapshots stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
        

# ==================== БЛОК 15: МОНИТОРИНГ КАМЕР И ОТЧЕТЫ ====================

from camera_monitor import get_monitor_manager
from daily_reporter import get_reporter

# Инициализируем менеджер мониторов
monitor_manager = get_monitor_manager()

# Инициализируем репортер (должен быть после monitor_manager)
reporter = get_reporter(monitor_manager, config_manager)

# Устанавливаем репортер для менеджера мониторов
monitor_manager.set_reporter(reporter)

# Автоматически добавляем все OpenIPC камеры из конфига
def init_camera_monitors():
    """Инициализировать мониторы для всех OpenIPC камер"""
    try:
        cameras = config_manager.get_cameras_list()
        added = 0
        
        for cam in cameras:
            if cam.get('type') == 'openipc':
                success = monitor_manager.add_camera(
                    camera_ip=cam['ip'],
                    username=cam.get('username', 'root'),
                    password=cam.get('password', '12345'),
                    rtsp_port=cam.get('rtsp_port', 554),
                    http_port=cam.get('port', 80)
                )
                if success:
                    added += 1
                    logger.info(f"✅ Added monitor for {cam['ip']} ({cam.get('name')})")
        
        logger.info(f"✅ Initialized {added} camera monitors")
    except Exception as e:
        logger.error(f"❌ Failed to initialize camera monitors: {e}")
        logger.exception(e)

# Запускаем инициализацию в отдельном потоке
threading.Thread(target=init_camera_monitors, daemon=True).start()


# ===== API ЭНДПОИНТЫ ДЛЯ МОНИТОРИНГА =====

@app.route('/api/camera_monitor/status', methods=['GET'])
def camera_monitor_status():
    """Получить статус всех мониторов камер"""
    try:
        camera_ip = request.args.get('camera')
        result = monitor_manager.get_status(camera_ip)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting monitor status: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/restart/<camera_ip>', methods=['POST'])
def camera_monitor_restart(camera_ip):
    """Принудительно перезапустить majestic на камере"""
    try:
        result = monitor_manager.restart_camera(camera_ip)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error restarting camera {camera_ip}: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/add', methods=['POST'])
def camera_monitor_add():
    """Добавить камеру в мониторинг вручную"""
    try:
        data = request.json
        if not data or 'camera_ip' not in data:
            return jsonify({'success': False, 'error': 'No camera_ip provided'}), 400
        
        success = monitor_manager.add_camera(
            camera_ip=data['camera_ip'],
            username=data.get('username', 'root'),
            password=data.get('password', '12345'),
            rtsp_port=data.get('rtsp_port', 554),
            http_port=data.get('http_port', 80)
        )
        
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"Error adding camera to monitor: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/remove/<camera_ip>', methods=['POST'])
def camera_monitor_remove(camera_ip):
    """Удалить камеру из мониторинга"""
    try:
        success = monitor_manager.remove_camera(camera_ip)
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"Error removing camera from monitor: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/stats', methods=['GET'])
def camera_monitor_stats():
    """Получить статистику по мониторингу"""
    try:
        status = monitor_manager.get_status()
        if status['success']:
            total = status['total']
            healthy = 0
            warning = 0
            unhealthy = 0
            total_restarts = 0
            
            for mon in status['monitors'].values():
                if mon['status'] == 'healthy':
                    healthy += 1
                elif mon['status'] == 'warning':
                    warning += 1
                else:
                    unhealthy += 1
                total_restarts += mon.get('restart_count', 0)
            
            return jsonify({
                'success': True,
                'total': total,
                'healthy': healthy,
                'warning': warning,
                'unhealthy': unhealthy,
                'restart_count': total_restarts
            })
        return jsonify({'success': False, 'error': 'Failed to get stats'})
    except Exception as e:
        logger.error(f"Error getting monitor stats: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== API ЭНДПОИНТЫ ДЛЯ ОТЧЕТОВ =====

@app.route('/api/camera_monitor/reports', methods=['GET'])
def camera_monitor_reports():
    """Получить историю отчетов"""
    try:
        limit = int(request.args.get('limit', 10))
        reports = reporter.get_reports_history(limit)
        return jsonify({
            'success': True,
            'reports': reports
        })
    except Exception as e:
        logger.error(f"Error getting reports: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/send_report', methods=['POST'])
def camera_monitor_send_report():
    """Принудительно отправить отчет"""
    try:
        reporter._send_daily_report()
        return jsonify({'success': True, 'message': 'Report sent'})
    except Exception as e:
        logger.error(f"Error sending report: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/camera_monitor/failures', methods=['GET'])
def camera_monitor_failures():
    """Получить историю сбоев за сегодня"""
    try:
        # Можно реализовать получение истории из репортера
        return jsonify({
            'success': True,
            'failures': []  # Заглушка, можно добавить позже
        })
    except Exception as e:
        logger.error(f"Error getting failures: {e}")
        logger.exception(e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== ВЕБ-ИНТЕРФЕЙС ДЛЯ МОНИТОРИНГА (ПЕРЕИМЕНОВАНО) =====

@app.route('/camera-monitor')  # ИЗМЕНЕНО: было '/monitor', стало '/camera-monitor'
def camera_monitor_page():      # ИЗМЕНЕНО: было 'monitor_page', стало 'camera_monitor_page'
    """Страница мониторинга камер"""
    return render_template('monitor.html')
# ==================== БЛОК 13: ЗАПУСК ПРИЛОЖЕНИЯ ====================

if __name__ == '__main__':
    logger.info("="*60)
    logger.info("Starting OpenIPC Bridge with HLS Streaming and RHVoice support")
    logger.info("="*60)
    
    # Конфигурация уже загружена при инициализации config_manager
    logger.info(f"✅ Configuration ready with {len(config_manager.config['cameras'])} cameras")
    
    # Очищаем debug файл при старте
    if config_manager.config['logging'].get('debug_qr', True):
        try:
            with open(QR_DEBUG_FILE, 'w', encoding='utf-8') as f:
                f.write(f"QR Debug started at {datetime.now()}\n")
        except Exception as e:
            logger.error(f"Failed to clear QR debug file: {e}")
    
    # ===== ИНИЦИАЛИЗАЦИЯ СИСТЕМЫ ЗАПИСИ =====
    logger.info("📹 Initializing Recording System...")
    try:
        init_recording_api(app)
        logger.info("✅ Recording API initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize recording API: {e}")
        logger.exception(e)
    
    # ===== ФУНКЦИЯ АВТОМАТИЧЕСКОГО ЗАПУСКА ЗАПИСИ =====
    def auto_start_recordings():
        """Запустить запись на всех камерах через 30 секунд после старта"""
        logger.info("⏳ Auto-recording will start in 30 seconds...")
        time.sleep(30)
        try:
            from recording_api import start_all_recordings
            start_all_recordings()
        except Exception as e:
            logger.error(f"Auto-start error: {e}")
            logger.exception(e)
    
    # Запускаем авто-запись в отдельном потоке
    threading.Thread(target=auto_start_recordings, daemon=True).start()
    
    # ===== ИНИЦИАЛИЗАЦИЯ STREAM MONITOR =====
    logger.info("🚀 Initializing Stream Monitor...")
    try:
        stream_monitor = init_stream_monitor(app, stream_managers, stop_event)
        logger.info("✅ Stream Monitor initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Stream Monitor: {e}")
        logger.exception(e)
    
    # Запускаем сервер
    logger.info(f"🚀 Starting Flask server on port 5000...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
