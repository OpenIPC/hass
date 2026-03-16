"""
API endpoints for recording management - С поддержкой настроек из конфига
"""

# ==================== БЛОК 0: ИМПОРТЫ ====================

import os
import json
import time
import shutil
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from collections import deque

from flask import Blueprint, request, jsonify, send_file

# Импортируем менеджер конфигурации
from config_manager import get_config_manager


# ==================== БЛОК 1: КОНФИГУРАЦИЯ И КОНСТАНТЫ ====================

# Настройка логирования
logger = logging.getLogger(__name__)

# Добавляем файловый логгер
log_file = '/config/recording_debug.log'
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

recording_bp = Blueprint('recording', __name__)

# Директории для хранения
RECORDINGS_DIR = "/config/www/recordings"
EXPORTS_DIR = "/config/www/exports"
SNAPSHOTS_DIR = "/config/www/snapshots"

# Создаем директории
try:
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(RECORDINGS_DIR, "thumbnails"), exist_ok=True)
    logger.info("✅ All recording directories created successfully")
except Exception as e:
    logger.error(f"❌ Failed to create directories: {e}")

# Проверяем права на запись
for dir_path in [RECORDINGS_DIR, EXPORTS_DIR, SNAPSHOTS_DIR]:
    try:
        test_file = os.path.join(dir_path, "write_test.tmp")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        logger.info(f"✅ Write permission OK for {dir_path}")
    except Exception as e:
        logger.error(f"❌ No write permission for {dir_path}: {e}")

# База данных записей
recordings_db: Dict[str, dict] = {}
active_recordings: Dict[str, dict] = {}

# Ограничение на количество одновременных записей
MAX_CONCURRENT_RECORDINGS = 5  # Максимум 5 камер одновременно для 8GB RAM




# ==================== БЛОК 2: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_camera_recording_settings(camera_ip):
    """Получить настройки записи для камеры из конфигурации"""
    try:
        config_manager = get_config_manager()
        logger.info(f"🔍 Getting recording settings for camera {camera_ip}")
        
        cam_config = config_manager.get_camera(camera_ip)
        
        if not cam_config:
            logger.warning(f"⚠️ No camera config found for {camera_ip}")
            return None
        
        # Настройки по умолчанию
        default = {
            'mode': 'continuous',
            'enabled': True,
            'segment_duration': 300,
            'archive_depth': 7,
            'quality': 'medium',
            'fps': 15,
            'format': 'mp4',
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
            'fps': recording_settings.get('fps', default['fps']),
            'format': recording_settings.get('format', default['format']),
            'detect_motion': detection_settings.get('motion', default['detect_motion']),
            'detect_qr': detection_settings.get('qr', default['detect_qr']),
            'sensitivity': detection_settings.get('sensitivity', 5)
        }
        
        logger.info(f"✅ Retrieved settings for {camera_ip}: mode={settings['mode']}, quality={settings['quality']}, fps={settings['fps']}")
        logger.debug(f"Full settings: {settings}")
        return settings
        
    except Exception as e:
        logger.error(f"❌ Error getting recording settings for {camera_ip}: {e}")
        logger.exception(e)
        return None


def save_camera_recording_settings(camera_ip, settings):
    """Сохранить настройки записи для камеры в конфигурацию"""
    try:
        config_manager = get_config_manager()
        
        logger.info(f"💾 Attempting to save recording settings for camera {camera_ip}")
        logger.debug(f"Settings to save: {settings}")
        
        # Обновляем настройки через менеджер
        success = config_manager.update_recording_settings(camera_ip, settings)
        
        if not success:
            logger.error(f"❌ Camera {camera_ip} not found in config")
            # Выводим список доступных камер для отладки
            available_cameras = [c.get('ip') for c in config_manager.get_cameras_list()]
            logger.info(f"📋 Available cameras in config: {available_cameras}")
            return False
        
        # Сохраняем конфигурацию
        logger.info("💾 Saving configuration to file...")
        if config_manager.save_config():
            logger.info(f"✅ Recording settings successfully saved for camera {camera_ip}")
            return True
        else:
            logger.error(f"❌ Failed to save config file for camera {camera_ip}")
            return False
        
    except Exception as e:
        logger.error(f"❌ Error saving recording settings for {camera_ip}: {e}")
        logger.exception(e)
        return False


def get_camera_type(camera_ip):
    """Получить тип камеры из конфигурации"""
    try:
        config_manager = get_config_manager()
        cam_config = config_manager.get_camera(camera_ip)
        if cam_config:
            camera_type = cam_config.get('type', 'openipc')
            logger.debug(f"Camera {camera_ip} type: {camera_type}")
            return camera_type
    except Exception as e:
        logger.error(f"Error getting camera type for {camera_ip}: {e}")
    
    logger.debug(f"Camera {camera_ip} type not found, defaulting to 'openipc'")
    return 'openipc'


def get_camera_credentials(camera_ip):
    """Получить логин/пароль камеры"""
    try:
        config_manager = get_config_manager()
        cam_config = config_manager.get_camera(camera_ip)
        if cam_config:
            username = cam_config.get('username', 'root')
            password = cam_config.get('password', '12345')
            logger.debug(f"Got credentials for {camera_ip}: username={username}")
            return username, password
    except Exception as e:
        logger.error(f"Error getting credentials for {camera_ip}: {e}")
    
    logger.debug(f"Using default credentials for {camera_ip}")
    return 'root', '12345'


def check_recordings_status():
    """Проверить статус всех активных записей"""
    try:
        import requests
        logger.info("🔍 Checking status of all active recordings...")
        
        response = requests.get("http://localhost:5000/api/recordings/status", timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                active = data.get('active_recordings', 0)
                recordings = data.get('recordings', {})
                
                logger.info(f"📊 Active recordings status: {active} active")
                for cam_ip, info in recordings.items():
                    if info.get('active'):
                        elapsed = info.get('elapsed', 0)
                        recording_id = info.get('recording_id', 'unknown')
                        logger.info(f"   ✅ {cam_ip}: recording for {elapsed:.0f}s (ID: {recording_id})")
                
                return {
                    'success': True,
                    'active': active,
                    'recordings': recordings
                }
            else:
                logger.warning(f"⚠️ Failed to get recordings status: {data.get('error')}")
                return {'success': False, 'error': data.get('error')}
        else:
            logger.warning(f"⚠️ Failed to get recordings status: HTTP {response.status_code}")
            return {'success': False, 'error': f'HTTP {response.status_code}'}
            
    except requests.exceptions.ConnectionError:
        logger.error("❌ Connection error while checking recordings status")
        return {'success': False, 'error': 'Connection error'}
    except Exception as e:
        logger.error(f"❌ Failed to check recordings status: {e}")
        return {'success': False, 'error': str(e)}


def get_recordings_summary():
    """Получить краткую сводку по всем записям"""
    try:
        total_size = 0
        total_duration = 0
        by_date = {}
        by_camera = {}
        
        for rec_id, metadata in recordings_db.items():
            # Общая статистика
            size = metadata.get('size', 0)
            duration = metadata.get('duration', 0)
            total_size += size
            total_duration += duration
            
            # По датам
            date = metadata.get('date', 'unknown')
            if date not in by_date:
                by_date[date] = {'count': 0, 'size': 0, 'duration': 0}
            by_date[date]['count'] += 1
            by_date[date]['size'] += size
            by_date[date]['duration'] += duration
            
            # По камерам
            camera = metadata.get('camera_ip', 'unknown')
            if camera not in by_camera:
                by_camera[camera] = {'count': 0, 'size': 0, 'duration': 0}
            by_camera[camera]['count'] += 1
            by_camera[camera]['size'] += size
            by_camera[camera]['duration'] += duration
        
        summary = {
            'total_recordings': len(recordings_db),
            'total_size_mb': total_size / (1024 * 1024),
            'total_duration_hours': total_duration / 3600,
            'by_date': by_date,
            'by_camera': by_camera,
            'active_recordings': len(active_recordings)
        }
        
        logger.debug(f"Recordings summary: {summary['total_recordings']} recordings, {summary['total_size_mb']:.1f} MB total")
        return summary
        
    except Exception as e:
        logger.error(f"Error getting recordings summary: {e}")
        return None


def cleanup_old_recordings(days_to_keep=7):
    """Удалить записи старше указанного количества дней"""
    try:
        cutoff_time = time.time() - (days_to_keep * 24 * 3600)
        deleted_count = 0
        freed_space = 0
        
        logger.info(f"🧹 Cleaning up recordings older than {days_to_keep} days")
        
        for rec_id, metadata in list(recordings_db.items()):
            if metadata['start_time'] < cutoff_time:
                # Удаляем видеофайл
                video_path = metadata.get('video_path')
                if video_path and os.path.exists(video_path):
                    file_size = os.path.getsize(video_path)
                    os.remove(video_path)
                    freed_space += file_size
                    logger.debug(f"Deleted video: {video_path}")
                
                # Удаляем метаданные
                meta_path = video_path + '.json' if video_path else None
                if meta_path and os.path.exists(meta_path):
                    os.remove(meta_path)
                    logger.debug(f"Deleted metadata: {meta_path}")
                
                # Удаляем из базы
                del recordings_db[rec_id]
                deleted_count += 1
        
        logger.info(f"✅ Cleaned up {deleted_count} old recordings, freed {freed_space / (1024*1024):.1f} MB")
        return {
            'success': True,
            'deleted': deleted_count,
            'freed_space_mb': freed_space / (1024 * 1024)
        }
        
    except Exception as e:
        logger.error(f"❌ Error cleaning up old recordings: {e}")
        return {'success': False, 'error': str(e)}


def get_recording_by_id(recording_id):
    """Получить запись по ID"""
    try:
        if recording_id in recordings_db:
            return recordings_db[recording_id]
        
        # Если нет в памяти, пробуем найти на диске
        for root, dirs, files in os.walk(RECORDINGS_DIR):
            for file in files:
                if file.endswith('.json'):
                    try:
                        filepath = os.path.join(root, file)
                        with open(filepath, 'r') as f:
                            metadata = json.load(f)
                            if metadata.get('recording_id') == recording_id:
                                recordings_db[recording_id] = metadata
                                logger.info(f"✅ Loaded recording {recording_id} from disk")
                                return metadata
                    except:
                        continue
        
        logger.warning(f"Recording {recording_id} not found")
        return None
        
    except Exception as e:
        logger.error(f"Error getting recording {recording_id}: {e}")
        return None


def validate_recording_settings(settings):
    """Проверить корректность настроек записи"""
    errors = []
    
    # Проверка режима записи
    valid_modes = ['continuous', 'motion', 'schedule', 'disabled']
    if settings.get('mode') not in valid_modes:
        errors.append(f"Invalid mode: {settings.get('mode')}. Must be one of {valid_modes}")
    
    # Проверка длительности сегмента
    segment_duration = settings.get('segment_duration', 300)
    if not isinstance(segment_duration, int) or segment_duration < 60 or segment_duration > 3600:
        errors.append(f"Invalid segment_duration: {segment_duration}. Must be between 60 and 3600")
    
    # Проверка глубины архива
    archive_depth = settings.get('archive_depth', 7)
    if not isinstance(archive_depth, int) or archive_depth < 1 or archive_depth > 365:
        errors.append(f"Invalid archive_depth: {archive_depth}. Must be between 1 and 365")
    
    # Проверка качества
    valid_qualities = ['high', 'medium', 'low']
    if settings.get('quality') not in valid_qualities:
        errors.append(f"Invalid quality: {settings.get('quality')}. Must be one of {valid_qualities}")
    
    # Проверка FPS
    fps = settings.get('fps', 15)
    if not isinstance(fps, int) or fps < 1 or fps > 30:
        errors.append(f"Invalid fps: {fps}. Must be between 1 and 30")
    
    # Проверка формата
    valid_formats = ['mp4', 'mkv']
    if settings.get('format') not in valid_formats:
        errors.append(f"Invalid format: {settings.get('format')}. Must be one of {valid_formats}")
    
    if errors:
        logger.warning(f"Settings validation failed: {errors}")
        return False, errors
    
    return True, []




# ==================== БЛОК 3: КЛАСС RECORDING MANAGER ====================

class RecordingManager(threading.Thread):
    """Менеджер записи видео для одной камеры с поддержкой разных типов"""
    
    def __init__(self, camera_ip: str, username: str, password: str, settings: dict, camera_type: str = 'openipc'):
        super().__init__()
        self.camera_ip = camera_ip
        self.username = username
        self.password = password
        self.settings = settings
        self.camera_type = camera_type
        self.recording_id = f"{camera_ip}_{int(time.time())}_{os.urandom(4).hex()}"
        self.start_time = time.time()
        self.stop_flag = threading.Event()
        self.ffmpeg_process = None
        self.events = []
        self.ffmpeg_errors = []
        
        logger.info(f"📹 Initializing RecordingManager for {camera_ip} (type: {camera_type})")
        logger.info(f"  - Recording ID: {self.recording_id}")
        logger.info(f"  - Settings: {settings}")
        
        # Создаем директорию для записи с датой
        date_str = datetime.now().strftime('%Y-%m-%d')
        self.recording_dir = os.path.join(RECORDINGS_DIR, date_str, camera_ip)
        
        try:
            os.makedirs(self.recording_dir, exist_ok=True)
            logger.info(f"✅ Created directory: {self.recording_dir}")
        except Exception as e:
            logger.error(f"❌ Failed to create directory {self.recording_dir}: {e}")
        
        # Путь к видеофайлу
        time_str = datetime.now().strftime('%H-%M-%S')
        self.video_path = os.path.join(
            self.recording_dir, 
            f"{time_str}.{settings.get('format', 'mp4')}"
        )
        logger.info(f"  - Output file: {self.video_path}")
    
    def run(self):
        """Основной поток записи"""
        logger.info(f"🚀 Starting recording thread for {self.camera_ip}")
        
        try:
            # Формируем команду FFmpeg
            ffmpeg_cmd = self._build_ffmpeg_cmd()
            cmd_str = ' '.join(ffmpeg_cmd)
            logger.info(f"FFmpeg command: {cmd_str}")
            
            # Убеждаемся, что директория существует
            os.makedirs(os.path.dirname(self.video_path), exist_ok=True)
            
            # Запускаем FFmpeg процесс
            logger.info(f"Starting FFmpeg process...")
            self.ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            logger.info(f"✅ FFmpeg started with PID: {self.ffmpeg_process.pid}")
            
            # Запускаем поток для чтения вывода
            def read_stderr():
                error_lines = []
                for line in self.ffmpeg_process.stderr:
                    line = line.strip()
                    if line:
                        if 'error' in line.lower():
                            logger.error(f"FFmpeg: {line}")
                            error_lines.append(line)
                        elif 'warning' in line.lower():
                            logger.warning(f"FFmpeg: {line}")
                        elif line not in ['Press [q] to stop', '[?] for help']:
                            logger.debug(f"FFmpeg: {line}")
                
                if error_lines:
                    self.ffmpeg_errors = error_lines
            
            threading.Thread(target=read_stderr, daemon=True).start()
            
            # Ждем указанное время
            timeout = self.settings.get('duration', 300)
            logger.info(f"Recording will run for {timeout} seconds")
            
            # Ждем либо timeout, либо сигнал остановки
            self.stop_flag.wait(timeout)
            
            # Останавливаем FFmpeg
            logger.info(f"Stopping FFmpeg...")
            if self.ffmpeg_process:
                self.ffmpeg_process.terminate()
                try:
                    self.ffmpeg_process.wait(timeout=5)
                    logger.info("✅ FFmpeg stopped gracefully")
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg didn't stop, killing...")
                    self.ffmpeg_process.kill()
                    self.ffmpeg_process.wait()
                    logger.info("✅ FFmpeg killed")
            
            # Проверяем результат
            if os.path.exists(self.video_path):
                file_size = os.path.getsize(self.video_path)
                logger.info(f"✅ Video file created: {self.video_path} ({file_size} bytes)")
                
                if file_size < 1000:
                    logger.error(f"❌ Video file too small ({file_size} bytes)")
                    if hasattr(self, 'ffmpeg_errors') and self.ffmpeg_errors:
                        for error in self.ffmpeg_errors[-5:]:
                            logger.error(f"FFmpeg error: {error}")
            else:
                logger.error(f"❌ Video file NOT created: {self.video_path}")
                if hasattr(self, 'ffmpeg_errors') and self.ffmpeg_errors:
                    for error in self.ffmpeg_errors[-5:]:
                        logger.error(f"FFmpeg error: {error}")
            
            # Сохраняем метаданные
            self._save_metadata()
            
        except Exception as e:
            logger.error(f"❌ Recording error for {self.camera_ip}: {e}", exc_info=True)
    
    def _build_ffmpeg_cmd(self) -> list:
        """Построение команды FFmpeg для записи с учетом типа камеры"""
        
        # Выбираем правильный RTSP путь в зависимости от типа камеры
        if self.camera_type == 'beward':
            rtsp_path = '/av0_0'
        elif self.camera_type == 'vivotek':
            rtsp_path = '/live.sdp'
        else:  # openipc
            rtsp_path = '/stream=0'
        
        rtsp_url = f"rtsp://{self.username}:{self.password}@{self.camera_ip}:554{rtsp_path}"
        
        # ОПТИМИЗИРОВАННЫЕ параметры для низкой нагрузки на CPU
        quality = self.settings.get('quality', 'medium')
        fps = self.settings.get('fps', 15)  # Получаем FPS из настроек
        
        # Базовые параметры для всех режимов
        video_params = []
        
        if quality == 'high':
            # Высокое качество, но все еще экономное
            video_params = [
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '23',
                '-tune', 'zerolatency',
                '-x264-params', f'keyint={fps*2}:min-keyint={fps}'
            ]
        elif quality == 'low':
            # Низкое качество - минимальная нагрузка
            video_params = [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-crf', '28',
                '-tune', 'fastdecode',
                '-x264-params', f'keyint={fps*3}:min-keyint={fps*2}'
            ]
        else:  # medium (по умолчанию)
            # Среднее качество - баланс
            video_params = [
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '25',
                '-tune', 'zerolatency',
                '-x264-params', f'keyint={fps*2}:min-keyint={fps}'
            ]
        
        # Ограничиваем FPS для снижения нагрузки
        video_params.extend(['-r', str(fps)])
        
        # Аудио параметры (экономные)
        audio_params = [
            '-c:a', 'aac',
            '-b:a', '32k',
            '-ar', '16000',
            '-ac', '1'
        ]
        
        # Базовая команда
        cmd = [
            'ffmpeg',
            '-y',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            *video_params,
            *audio_params,
            '-t', str(self.settings.get('duration', 300)),
            '-movflags', '+faststart',
            self.video_path
        ]
        
        return cmd
    
    def add_event(self, event_type: str, data: dict):
        """Добавление события"""
        event = {
            'time': time.time() - self.start_time,
            'type': event_type,
            'data': data
        }
        self.events.append(event)
        logger.debug(f"Event added: {event_type} at {event['time']:.2f}s")
        
    def _save_metadata(self):
        """Сохранение метаданных записи"""
        end_time = time.time()
        duration = end_time - self.start_time
        
        logger.info(f"Saving metadata for recording {self.recording_id}")
        logger.info(f"  - Duration: {duration:.2f}s")
        
        # Проверяем, создался ли файл
        if not os.path.exists(self.video_path):
            logger.error(f"❌ Video file not created: {self.video_path}")
            return
            
        file_size = os.path.getsize(self.video_path)
        logger.info(f"  - File size: {file_size} bytes ({file_size/1024/1024:.2f} MB)")
        
        metadata = {
            'recording_id': self.recording_id,
            'camera_ip': self.camera_ip,
            'camera_type': self.camera_type,
            'start_time': self.start_time,
            'end_time': end_time,
            'duration': duration,
            'settings': self.settings,
            'events': self.events,
            'video_path': self.video_path,
            'size': file_size,
            'filename': os.path.basename(self.video_path),
            'date': datetime.now().strftime('%Y-%m-%d')
        }
        
        # Сохраняем метаданные в JSON файл
        meta_path = self.video_path + '.json'
        try:
            with open(meta_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            logger.info(f"✅ Metadata saved: {meta_path}")
        except Exception as e:
            logger.error(f"❌ Failed to save metadata: {e}")
        
        # Добавляем в общую БД
        recordings_db[self.recording_id] = metadata
        logger.info(f"✅ Recording added to database. Total recordings: {len(recordings_db)}")
        
    def stop(self):
        """Остановка записи"""
        logger.info(f"Stopping recording for {self.camera_ip}")
        self.stop_flag.set()

# ==================== БЛОК 4: API ЭНДПОИНТЫ - ЗАПИСЬ ====================

@recording_bp.route('/api/recording/start', methods=['POST'])
def start_recording():
    """Начать запись с камеры"""
    logger.info("=" * 60)
    logger.info("🎬 START RECORDING API CALLED")
    
    try:
        data = request.json
        camera_ip = data.get('camera')
        
        if not camera_ip:
            logger.error("❌ No camera IP provided")
            return jsonify({'success': False, 'error': 'Camera IP required'}), 400
        
        # Получаем лимит из конфига
        config_manager = get_config_manager()
        max_recordings = config_manager.get_max_recordings()
        
        # Проверяем количество активных записей
        if len(active_recordings) >= max_recordings:
            logger.warning(f"⚠️ Maximum recordings reached ({max_recordings})")
            return jsonify({
                'success': False, 
                'error': f'Maximum recordings reached ({max_recordings}). Stop some recordings first.'
            }), 400
        
        # Получаем настройки камеры из конфига
        recording_settings = get_camera_recording_settings(camera_ip)
        
        if not recording_settings:
            logger.error(f"Camera not found or no settings: {camera_ip}")
            return jsonify({'success': False, 'error': 'Camera not found'}), 404
        
        # Проверяем, включена ли запись для этой камеры
        if not recording_settings.get('enabled', True):
            logger.info(f"Recording disabled for {camera_ip}, skipping")
            return jsonify({'success': False, 'error': 'Recording disabled for this camera'}), 400
        
        # Проверяем, не идет ли уже запись
        if camera_ip in active_recordings:
            logger.warning(f"Recording already active for {camera_ip}")
            return jsonify({'success': False, 'error': 'Recording already active'}), 400
        
        # Получаем логин/пароль
        username, password = get_camera_credentials(camera_ip)
        
        # Определяем тип камеры
        camera_type = get_camera_type(camera_ip)
        
        # Определяем длительность записи в зависимости от режима
        duration = data.get('duration')
        if not duration:
            if recording_settings['mode'] == 'continuous':
                duration = 86400  # 24 часа
            elif recording_settings['mode'] == 'motion':
                duration = 300  # 5 минут, потом перезапустится если есть движение
            else:
                duration = recording_settings['segment_duration']
        
        settings = {
            'mode': recording_settings['mode'],
            'duration': duration,
            'segment_duration': recording_settings['segment_duration'],
            'archive_depth': recording_settings['archive_depth'],
            'quality': recording_settings['quality'],
            'fps': recording_settings['fps'],
            'format': recording_settings['format'],
            'detect_motion': recording_settings['detect_motion'],
            'detect_qr': recording_settings['detect_qr'],
            'copy_video': True
        }
        
        logger.info(f"Recording settings for {camera_ip}: {settings}")
        
        # Создаем и запускаем менеджер записи
        manager = RecordingManager(
            camera_ip=camera_ip,
            username=username,
            password=password,
            settings=settings,
            camera_type=camera_type
        )
        
        manager.start()
        active_recordings[camera_ip] = manager
        
        logger.info(f"✅ Recording started for {camera_ip}")
        
        return jsonify({
            'success': True,
            'recording_id': manager.recording_id,
            'file': manager.video_path,
            'mode': settings['mode']
        })
        
    except Exception as e:
        logger.error(f"❌ Error starting recording: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        logger.info("=" * 60)


@recording_bp.route('/api/recording/stop', methods=['POST'])
def stop_recording():
    """Остановить запись"""
    logger.info("=" * 60)
    logger.info("🛑 STOP RECORDING API CALLED")
    
    try:
        data = request.json
        camera_ip = data.get('camera')
        
        if camera_ip in active_recordings:
            logger.info(f"Stopping recording for {camera_ip}")
            active_recordings[camera_ip].stop()
            active_recordings[camera_ip].join(timeout=10)
            del active_recordings[camera_ip]
            logger.info(f"✅ Recording stopped")
            return jsonify({'success': True})
        
        logger.warning(f"No active recording for {camera_ip}")
        return jsonify({'success': False, 'error': 'No active recording'}), 404
        
    except Exception as e:
        logger.error(f"❌ Error stopping recording: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        logger.info("=" * 60)


@recording_bp.route('/api/recording/settings/<camera_ip>', methods=['GET', 'POST'])
def recording_settings(camera_ip):
    """Получить или обновить настройки записи для камеры"""
    if request.method == 'GET':
        settings = get_camera_recording_settings(camera_ip)
        if settings:
            return jsonify({'success': True, 'settings': settings})
        return jsonify({'success': False, 'error': 'Camera not found'}), 404
    
    elif request.method == 'POST':
        data = request.json
        success = save_camera_recording_settings(camera_ip, data)
        if success:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Failed to save settings'}), 500


@recording_bp.route('/api/recordings/restart_all', methods=['POST'])
def restart_all_recordings():
    """Перезапустить все активные записи"""
    logger.info("=" * 60)
    logger.info("🔄 RESTART ALL RECORDINGS API CALLED")
    
    try:
        # Останавливаем все записи
        stopped = []
        for camera_ip, manager in list(active_recordings.items()):
            logger.info(f"Stopping recording for {camera_ip}")
            manager.stop()
            manager.join(timeout=10)
            stopped.append(camera_ip)
            del active_recordings[camera_ip]
        
        # Ждем немного
        time.sleep(3)
        
        # Запускаем все записи заново
        started = []
        for camera_ip in stopped:
            try:
                # Получаем настройки камеры
                recording_settings = get_camera_recording_settings(camera_ip)
                if not recording_settings or not recording_settings.get('enabled', True):
                    continue
                
                # Получаем логин/пароль
                username, password = get_camera_credentials(camera_ip)
                camera_type = get_camera_type(camera_ip)
                
                settings = {
                    'mode': recording_settings['mode'],
                    'duration': 300 if recording_settings['mode'] == 'motion' else 86400,
                    'segment_duration': recording_settings['segment_duration'],
                    'archive_depth': recording_settings['archive_depth'],
                    'quality': recording_settings['quality'],
                    'fps': recording_settings['fps'],
                    'format': recording_settings['format'],
                    'detect_motion': recording_settings['detect_motion'],
                    'detect_qr': recording_settings['detect_qr'],
                    'copy_video': True
                }
                
                manager = RecordingManager(
                    camera_ip=camera_ip,
                    username=username,
                    password=password,
                    settings=settings,
                    camera_type=camera_type
                )
                
                manager.start()
                active_recordings[camera_ip] = manager
                started.append(camera_ip)
                logger.info(f"✅ Restarted recording for {camera_ip}")
                
            except Exception as e:
                logger.error(f"❌ Failed to restart {camera_ip}: {e}")
        
        logger.info(f"✅ Restarted {len(started)} recordings")
        return jsonify({
            'success': True,
            'restarted': started,
            'total': len(started)
        })
        
    except Exception as e:
        logger.error(f"❌ Error restarting recordings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        logger.info("=" * 60)


@recording_bp.route('/api/recordings/stop_all', methods=['POST'])
def stop_all_recordings():
    """Остановить все активные записи"""
    logger.info("=" * 60)
    logger.info("🛑 STOP ALL RECORDINGS API CALLED")
    
    try:
        stopped = []
        for camera_ip, manager in list(active_recordings.items()):
            logger.info(f"Stopping recording for {camera_ip}")
            manager.stop()
            manager.join(timeout=10)
            stopped.append(camera_ip)
            del active_recordings[camera_ip]
        
        logger.info(f"✅ Stopped {len(stopped)} recordings")
        return jsonify({
            'success': True,
            'stopped': stopped
        })
        
    except Exception as e:
        logger.error(f"❌ Error stopping recordings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        logger.info("=" * 60)




# ==================== БЛОК 5: API ЭНДПОИНТЫ - ЗАПРОСЫ ====================
# (без изменений)

@recording_bp.route('/api/recordings', methods=['GET'])
def get_recordings():
    """Получить список записей"""
    logger.debug("GET /api/recordings called")
    
    try:
        camera = request.args.get('camera', 'all')
        date = request.args.get('date')
        limit = int(request.args.get('limit', 100))
        
        recordings = []
        
        for rec_id, metadata in recordings_db.items():
            # Фильтр по камере
            if camera != 'all' and metadata['camera_ip'] != camera:
                continue
                
            # Фильтр по дате
            if date:
                rec_date = datetime.fromtimestamp(metadata['start_time']).strftime('%Y-%m-%d')
                if rec_date != date:
                    continue
            
            rec_info = {
                'id': rec_id,
                'camera': metadata['camera_ip'],
                'start': metadata['start_time'],
                'end': metadata['end_time'],
                'duration': int(metadata['duration']),
                'size': metadata.get('size', 0),
                'filename': metadata.get('filename', ''),
                'date': metadata.get('date', ''),
                'has_motion': any(e['type'] == 'motion' for e in metadata.get('events', [])),
                'has_qr': any(e['type'] == 'qr' for e in metadata.get('events', [])),
                'events': metadata.get('events', [])
            }
            
            recordings.append(rec_info)
        
        recordings.sort(key=lambda x: x['start'], reverse=True)
        
        logger.debug(f"Returning {len(recordings)} recordings")
        
        return jsonify({
            'success': True,
            'recordings': recordings[:limit]
        })
        
    except Exception as e:
        logger.error(f"Error getting recordings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recordings/stats', methods=['GET'])
def get_recording_stats():
    """Получить статистику записей"""
    try:
        total_size = 0
        total_duration = 0
        events_count = {'motion': 0, 'qr': 0, 'object': 0}
        
        for metadata in recordings_db.values():
            total_size += metadata.get('size', 0)
            total_duration += metadata.get('duration', 0)
            
            for event in metadata.get('events', []):
                if event['type'] in events_count:
                    events_count[event['type']] += 1
        
        return jsonify({
            'success': True,
            'total_recordings': len(recordings_db),
            'total_size': total_size,
            'total_duration': total_duration,
            'events': events_count,
            'active_recordings': len(active_recordings)
        })
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recording/<recording_id>', methods=['GET'])
def get_recording(recording_id):
    """Получить конкретную запись"""
    try:
        if recording_id not in recordings_db:
            return jsonify({'success': False, 'error': 'Recording not found'}), 404
        
        metadata = recordings_db[recording_id]
        
        # Формируем URL для доступа к видео
        video_path = metadata['video_path']
        relative_path = video_path.replace('/config/www/', '')
        video_url = f"/{relative_path}"
        
        return jsonify({
            'success': True,
            'url': video_url,
            'metadata': metadata
        })
        
    except Exception as e:
        logger.error(f"Error getting recording: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recording/<recording_id>/download', methods=['GET'])
def download_recording(recording_id):
    """Скачать запись"""
    try:
        if recording_id not in recordings_db:
            return jsonify({'success': False, 'error': 'Recording not found'}), 404
        
        metadata = recordings_db[recording_id]
        video_path = metadata.get('video_path')
        
        if not video_path or not os.path.exists(video_path):
            return jsonify({'success': False, 'error': 'Video file not found'}), 404
        
        return send_file(
            video_path,
            as_attachment=True,
            download_name=metadata.get('filename', 'recording.mp4')
        )
        
    except Exception as e:
        logger.error(f"Error downloading recording: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recording/<recording_id>/marks', methods=['GET'])
def get_recording_marks(recording_id):
    """Получить метки событий для записи"""
    try:
        if recording_id not in recordings_db:
            return jsonify({'success': False, 'error': 'Recording not found'}), 404
        
        metadata = recordings_db[recording_id]
        events = metadata.get('events', [])
        duration = metadata['duration']
        
        marks = []
        for event in events:
            mark = {
                'time': event['time'],
                'type': event['type']
            }
            
            if event['type'] == 'qr':
                mark['data'] = event['data'].get('code', '')
            elif event['type'] == 'motion':
                mark['data'] = f"Чувствительность: {event['data'].get('sensitivity', '')}"
            
            marks.append(mark)
        
        return jsonify({
            'success': True,
            'duration': duration,
            'marks': marks
        })
        
    except Exception as e:
        logger.error(f"Error getting marks: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recordings/status', methods=['GET'])
def get_recordings_status():
    """Получить статус всех записей"""
    try:
        status = {}
        for camera_ip, manager in active_recordings.items():
            status[camera_ip] = {
                'active': True,
                'recording_id': manager.recording_id,
                'start_time': manager.start_time,
                'elapsed': time.time() - manager.start_time,
                'file': manager.video_path
            }
        return jsonify({
            'success': True,
            'active_recordings': len(active_recordings),
            'recordings': status
        })
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@recording_bp.route('/api/recordings/cleanup', methods=['POST'])
def cleanup_recordings():
    """Удалить старые записи согласно настройкам"""
    try:
        # Получаем глубину архива из настроек первой камеры (или можно сделать глобальную)
        archive_depth = 7
        cutoff = time.time() - (archive_depth * 24 * 3600)
        
        deleted = []
        for rec_id, metadata in list(recordings_db.items()):
            if metadata['start_time'] < cutoff:
                # Удаляем видеофайл
                video_path = metadata.get('video_path')
                if video_path and os.path.exists(video_path):
                    os.remove(video_path)
                
                # Удаляем метаданные
                meta_path = video_path + '.json'
                if os.path.exists(meta_path):
                    os.remove(meta_path)
                
                deleted.append(rec_id)
                del recordings_db[rec_id]
        
        logger.info(f"Cleaned up {len(deleted)} old recordings")
        
        return jsonify({
            'success': True,
            'deleted': len(deleted)
        })
        
    except Exception as e:
        logger.error(f"Error cleaning up: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== БЛОК 6: API ЭНДПОИНТЫ - СНИМКИ ====================
# (без изменений)

@recording_bp.route('/api/snapshots/save', methods=['POST'])
def save_snapshot():
    """Сохранить снимок"""
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file'}), 400
        
        image_file = request.files['image']
        camera = request.form.get('camera', 'unknown')
        snapshot_type = request.form.get('type', 'manual')
        timestamp = request.form.get('timestamp', str(int(time.time())))
        
        # Очищаем имя камеры
        camera_folder = camera.replace('.', '_').replace(' ', '_').lower()
        
        # Создаем структуру папок
        date_str = datetime.now().strftime('%Y-%m-%d')
        snapshot_dir = os.path.join(SNAPSHOTS_DIR, date_str, camera_folder, snapshot_type)
        os.makedirs(snapshot_dir, exist_ok=True)
        
        # Генерируем имя файла
        time_str = datetime.now().strftime('%H-%M-%S')
        filename = f"{time_str}.jpg"
        filepath = os.path.join(snapshot_dir, filename)
        
        # Сохраняем файл
        image_file.save(filepath)
        
        # Создаем уменьшенную копию
        try:
            from PIL import Image
            img = Image.open(filepath)
            img.thumbnail((320, 240))
            thumb_path = os.path.join(snapshot_dir, f"thumb_{filename}")
            img.save(thumb_path, 'JPEG', quality=85)
        except Exception as e:
            logger.error(f"Error creating thumbnail: {e}")
        
        # Формируем URL
        relative_path = filepath.replace('/config/www/', '')
        url = f"/{relative_path}"
        
        logger.info(f"✅ Snapshot saved: {filepath}")
        
        return jsonify({
            'success': True,
            'path': filepath,
            'url': url,
            'filename': filename
        })
        
    except Exception as e:
        logger.error(f"Error saving snapshot: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== БЛОК 7: АВТОМАТИЧЕСКИЙ ЗАПУСК ====================
# (обновлено использование config_manager)

def start_all_recordings():
    """Автоматический запуск записи на ВСЕХ камерах согласно настройкам"""
    logger.info("=" * 60)
    logger.info("🎬 Auto-starting recordings for ALL cameras")
    logger.info("=" * 60)
    
    try:
        config_manager = get_config_manager()
        
        # Получаем список ВСЕХ камер из конфига
        cameras = config_manager.get_cameras_list(include_status=True)
        
        logger.info(f"📸 Found {len(cameras)} cameras in config")
        
        started = 0
        skipped = 0
        failed = 0
        
        for camera in cameras:
            camera_ip = camera['ip']
            camera_name = camera['name']
            camera_online = camera.get('online', False)
            
            logger.info(f"🔍 Processing camera: {camera_name} ({camera_ip}) - online: {camera_online}")
            
            # Получаем настройки записи для камеры
            settings = get_camera_recording_settings(camera_ip)
            
            if not settings:
                logger.warning(f"⚠️ No recording settings for {camera_ip}, skipping")
                skipped += 1
                continue
            
            if not settings.get('enabled', True):
                logger.info(f"⏸️ Recording disabled for {camera_ip} in config, skipping")
                skipped += 1
                continue
            
            if not camera_online:
                logger.warning(f"⚠️ Camera {camera_ip} is offline, skipping")
                skipped += 1
                continue
            
            # Запускаем запись
            try:
                import requests
                logger.info(f"🎬 Starting recording on {camera_name} ({camera_ip})...")
                
                response = requests.post(
                    "http://localhost:5000/api/recording/start",
                    json={"camera": camera_ip},
                    timeout=10
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('success'):
                        logger.info(f"✅ SUCCESS: Recording started on {camera_name} ({camera_ip})")
                        started += 1
                    else:
                        logger.error(f"❌ FAILED: {camera_name} ({camera_ip}) - {result.get('error', 'Unknown error')}")
                        failed += 1
                else:
                    logger.error(f"❌ HTTP ERROR {response.status_code} for {camera_ip}")
                    failed += 1
                    
            except requests.exceptions.ConnectionError:
                logger.error(f"❌ Connection error for {camera_ip} - is the server running?")
                failed += 1
            except requests.exceptions.Timeout:
                logger.error(f"❌ Timeout for {camera_ip}")
                failed += 1
            except Exception as e:
                logger.error(f"❌ Unexpected error for {camera_ip}: {e}")
                failed += 1
            
            # Пауза между запусками, чтобы не перегружать систему
            time.sleep(2)
        
        logger.info("=" * 60)
        logger.info(f"📊 Auto-start summary:")
        logger.info(f"   ✅ Started: {started}")
        logger.info(f"   ⏸️ Skipped: {skipped}")
        logger.info(f"   ❌ Failed: {failed}")
        logger.info(f"   📸 Total cameras in config: {len(cameras)}")
        logger.info("=" * 60)
        
        # Проверяем статус всех записей после запуска
        time.sleep(5)
        check_recordings_status()
        
    except Exception as e:
        logger.error(f"❌ Critical error in auto-start: {e}")
        logger.exception(e)
    
    logger.info("🎬 Auto-start completed")
    logger.info("=" * 60)


# ==================== БЛОК 8: МОНИТОРИНГ РЕСУРСОВ ====================

@recording_bp.route('/api/recordings/resources', methods=['GET'])
def get_resource_usage():
    """Получить информацию об использовании ресурсов"""
    try:
        import psutil
        
        # Использование CPU
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # Использование памяти
        memory = psutil.virtual_memory()
        
        # Информация о процессах FFmpeg
        ffmpeg_processes = []
        for camera_ip, manager in active_recordings.items():
            if manager.ffmpeg_process:
                try:
                    process = psutil.Process(manager.ffmpeg_process.pid)
                    cpu_usage = process.cpu_percent(interval=0.1)
                    memory_usage = process.memory_info().rss / 1024 / 1024  # MB
                    ffmpeg_processes.append({
                        'camera': camera_ip,
                        'pid': manager.ffmpeg_process.pid,
                        'cpu': round(cpu_usage, 1),
                        'memory': round(memory_usage, 1)
                    })
                except psutil.NoSuchProcess:
                    logger.debug(f"Process {manager.ffmpeg_process.pid} no longer exists")
                except Exception as e:
                    logger.debug(f"Error getting process info: {e}")
        
        return jsonify({
            'success': True,
            'cpu_percent': cpu_percent,
            'memory': {
                'total': round(memory.total / 1024 / 1024 / 1024, 1),  # GB
                'available': round(memory.available / 1024 / 1024 / 1024, 1),  # GB
                'used': round((memory.total - memory.available) / 1024 / 1024 / 1024, 1),  # GB
                'percent': memory.percent
            },
            'active_recordings': len(active_recordings),
            'max_recordings': MAX_CONCURRENT_RECORDINGS,
            'ffmpeg_processes': ffmpeg_processes
        })
    except ImportError:
        logger.error("psutil not installed. Install with: pip install psutil")
        return jsonify({
            'success': False, 
            'error': 'psutil not installed. Resource monitoring unavailable.'
        }), 500
    except Exception as e:
        logger.error(f"Error getting resource usage: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== БЛОК 9: ИНИЦИАЛИЗАЦИЯ ====================
# (без изменений)

def init_recording_api(app):
    """Инициализация API записи"""
    logger.info("=" * 60)
    logger.info("📹 Initializing Recording API")
    
    # Загружаем существующие записи
    logger.info(f"Scanning for existing recordings in {RECORDINGS_DIR}")
    count = 0
    
    for root, dirs, files in os.walk(RECORDINGS_DIR):
        for file in files:
            if file.endswith('.json'):
                try:
                    filepath = os.path.join(root, file)
                    with open(filepath, 'r') as f:
                        metadata = json.load(f)
                        recordings_db[metadata['recording_id']] = metadata
                        count += 1
                except Exception as e:
                    logger.error(f"Error loading metadata {file}: {e}")
    
    logger.info(f"✅ Loaded {count} recordings from disk")
    logger.info(f"📹 Recording API initialized successfully")
    logger.info("=" * 60)
    
    # Регистрируем blueprint
    app.register_blueprint(recording_bp)
    
    # Добавляем эндпоинт для принудительного запуска всех записей
    @app.route('/api/recordings/start_all', methods=['POST'])
    def api_start_all_recordings():
        """API эндпоинт для принудительного запуска всех записей"""
        try:
            thread = threading.Thread(target=start_all_recordings)
            thread.daemon = True
            thread.start()
            return jsonify({"success": True, "message": "Starting all recordings"})
        except Exception as e:
            logger.error(f"Error starting all recordings: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
    
    return recording_bp