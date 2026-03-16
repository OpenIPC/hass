#!/usr/bin/env python3
"""
Stream Monitor for OpenIPC Bridge
Отдельный поток для мониторинга всех HLS потоков и автоматического восстановления
"""

import threading
import time
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Optional
from collections import deque

logger = logging.getLogger(__name__)

class StreamHealth:
    """Класс для хранения состояния здоровья потока"""
    
    def __init__(self, manager):
        self.manager = manager
        self.restart_count = 0
        self.error_count = 0
        self.last_restart = 0
        self.last_segment_time = 0
        self.segment_history = deque(maxlen=10)  # последние 10 сегментов
        self.error_history = deque(maxlen=20)    # последние 20 ошибок
        self.last_check = time.time()
        self.status = "unknown"
        self.consecutive_failures = 0
        self.recovery_attempts = 0
        self.max_recovery_attempts = 5
        self.backoff_time = 1  # начальная задержка между попытками (сек)
        
    def record_success(self):
        """Записать успешную проверку"""
        self.consecutive_failures = 0
        self.recovery_attempts = 0
        self.backoff_time = 1
        self.status = "healthy"
        self.last_check = time.time()
        
    def record_error(self, error_msg: str):
        """Записать ошибку"""
        self.consecutive_failures += 1
        self.error_count += 1
        self.error_history.append({
            'time': time.time(),
            'error': error_msg
        })
        self.status = "unhealthy"
        
    def should_restart(self) -> bool:
        """Проверить, нужно ли перезапустить поток"""
        if self.consecutive_failures >= 3:
            return True
            
        # Проверяем, не завис ли процесс
        if self.manager.process and self.manager.process.poll() is not None:
            return True
            
        # Проверяем время последнего сегмента
        if self.last_segment_time > 0:
            age = time.time() - self.last_segment_time
            if age > 30:  # нет сегментов больше 30 секунд
                return True
                
        return False
        
    def get_recovery_delay(self) -> float:
        """Получить задержку перед следующей попыткой восстановления (экспоненциальная задержка)"""
        self.recovery_attempts += 1
        delay = self.backoff_time * (2 ** (self.recovery_attempts - 1))
        return min(delay, 60)  # максимум 60 секунд


class StreamMonitor(threading.Thread):
    """
    Отдельный поток для мониторинга всех HLS потоков
    Автоматически обнаруживает проблемы и перезапускает потоки
    """
    
    def __init__(self, stream_managers: Dict, stop_event: threading.Event):
        super().__init__(name="stream_monitor")
        self.stream_managers = stream_managers
        self.stop_event = stop_event
        self.health_stats: Dict[str, StreamHealth] = {}
        self.daemon = True  # Поток завершится при завершении главного
        
        # Статистика для веб-интерфейса
        self.global_stats = {
            'total_restarts': 0,
            'total_errors': 0,
            'uptime': time.time(),
            'monitored_streams': 0
        }
        
        # Настройки мониторинга
        self.check_interval = 2  # проверка каждые 2 секунды
        self.playlist_check_interval = 5  # проверка плейлиста каждые 5 секунд
        self.stats_log_interval = 300  # логировать статистику каждые 5 минут
        
        # Кэш для проверки плейлистов
        self.playlist_cache = {}
        
    def run(self):
        """Основной цикл мониторинга"""
        logger.info("🚀 Stream Monitor started")
        
        last_stats_log = time.time()
        last_playlist_check = time.time()
        
        while not self.stop_event.is_set():
            try:
                current_time = time.time()
                
                # 1. Проверка всех потоков
                self._check_streams()
                
                # 2. Периодическая проверка плейлистов
                if current_time - last_playlist_check >= self.playlist_check_interval:
                    self._check_playlists()
                    last_playlist_check = current_time
                
                # 3. Периодическое логирование статистики
                if current_time - last_stats_log >= self.stats_log_interval:
                    self._log_stats()
                    last_stats_log = current_time
                
                # 4. Очистка зависших потоков
                self._cleanup_stale()
                
            except Exception as e:
                logger.error(f"Error in Stream Monitor: {e}")
            
            # Ждем следующий цикл
            self.stop_event.wait(self.check_interval)
        
        logger.info("🛑 Stream Monitor stopped")
    
    def _check_streams(self):
        """Проверка всех потоков"""
        for name, manager in list(self.stream_managers.items()):
            try:
                # Получаем или создаем статистику здоровья
                if name not in self.health_stats:
                    self.health_stats[name] = StreamHealth(manager)
                
                health = self.health_stats[name]
                
                # Проверяем процесс
                if manager.process is None:
                    health.record_error("Process not running")
                    
                    if health.should_restart():
                        self._restart_stream(name, manager, health)
                    continue
                    
                # Проверяем, жив ли процесс
                if manager.process.poll() is not None:
                    health.record_error(f"Process died with code {manager.process.returncode}")
                    
                    # Читаем последние строки лога
                    log_tail = self._read_last_log_lines(manager.log_file, 20)
                    logger.error(f"FFmpeg log for {name}:\n{log_tail}")
                    
                    if health.should_restart():
                        self._restart_stream(name, manager, health)
                    continue
                
                # Проверяем наличие сегментов
                segments = self._get_segments(manager.hls_dir)
                if segments:
                    latest_segment = max(segments, key=lambda s: os.path.getmtime(s))
                    segment_time = os.path.getmtime(latest_segment)
                    
                    if segment_time > health.last_segment_time:
                        health.last_segment_time = segment_time
                        health.segment_history.append(segment_time)
                    
                    # Проверяем возраст последнего сегмента
                    age = time.time() - segment_time
                    if age > 30:
                        health.record_error(f"No new segments for {age:.0f}s")
                        
                        if health.should_restart():
                            self._restart_stream(name, manager, health)
                        continue
                
                # Если дошли сюда - все хорошо
                health.record_success()
                
            except Exception as e:
                logger.error(f"Error checking stream {name}: {e}")
    
    def _check_playlists(self):
        """Проверка HLS плейлистов на доступность"""
        for name, manager in list(self.stream_managers.items()):
            try:
                playlist_path = manager.playlist_path
                
                if not os.path.exists(playlist_path):
                    logger.warning(f"Playlist not found for {name}")
                    continue
                
                # Проверяем время последнего изменения
                mtime = os.path.getmtime(playlist_path)
                age = time.time() - mtime
                
                # Сохраняем в кэш
                self.playlist_cache[name] = {
                    'mtime': mtime,
                    'age': age,
                    'size': os.path.getsize(playlist_path)
                }
                
                # Если плейлист не обновлялся больше минуты - проблема
                if age > 60:
                    logger.warning(f"Playlist for {name} stale ({age:.0f}s old)")
                    
            except Exception as e:
                logger.error(f"Error checking playlist {name}: {e}")
    
    def _restart_stream(self, name: str, manager, health: StreamHealth):
        """Перезапуск потока с экспоненциальной задержкой"""
        try:
            delay = health.get_recovery_delay()
            logger.warning(f"Restarting stream {name} (attempt {health.recovery_attempts}/{health.max_recovery_attempts}, delay={delay:.1f}s)")
            
            # Проверяем, не превышен ли лимит попыток
            if health.recovery_attempts > health.max_recovery_attempts:
                logger.error(f"Stream {name} failed to recover after {health.max_recovery_attempts} attempts")
                return
            
            # Останавливаем старый менеджер
            manager.stop()
            time.sleep(1)
            
            # Запускаем новый
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
        """Очистка зависших или мертвых потоков"""
        current_time = time.time()
        
        for name, manager in list(self.stream_managers.items()):
            try:
                # Если процесс мертв и не восстанавливается больше 5 минут
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
        """Получить список сегментов в директории"""
        try:
            if os.path.exists(hls_dir):
                return [os.path.join(hls_dir, f) for f in os.listdir(hls_dir) 
                       if f.startswith('segment_') and f.endswith('.ts')]
        except Exception:
            pass
        return []
    
    def _read_last_log_lines(self, log_file: str, num_lines: int) -> str:
        """Прочитать последние строки из лог-файла"""
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    return ''.join(lines[-num_lines:])
        except Exception:
            pass
        return "Log file not found"
    
    def _log_stats(self):
        """Логирование статистики"""
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
        """Форматировать время работы"""
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
        """Получить статус потока (для API)"""
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
            # Возвращаем статус всех потоков
            status = {}
            for name, health in self.health_stats.items():
                status[name] = {
                    'status': health.status,
                    'restarts': health.restart_count,
                    'errors': health.error_count
                }
            return status


# ==================== ИНТЕГРАЦИЯ С FLASK ====================

def init_stream_monitor(app, stream_managers, stop_event):
    """Инициализация монитора и добавление API эндпоинтов"""
    
    # Создаем и запускаем монитор
    monitor = StreamMonitor(stream_managers, stop_event)
    monitor.start()
    
    # Сохраняем в app для доступа из эндпоинтов
    app.stream_monitor = monitor
    
    @app.route('/api/monitor/status')
    def monitor_status():
        """Статус монитора и всех потоков"""
        return jsonify({
            'success': True,
            'monitor_running': monitor.is_alive(),
            'global_stats': monitor.global_stats,
            'streams': monitor.get_stream_status(),
            'playlist_cache': monitor.playlist_cache
        })
    
    @app.route('/api/monitor/stream/<stream_name>')
    def monitor_stream_status(stream_name):
        """Статус конкретного потока"""
        status = monitor.get_stream_status(stream_name)
        if status:
            return jsonify({'success': True, 'stream': status})
        return jsonify({'success': False, 'error': 'Stream not found'}), 404
    
    @app.route('/api/monitor/restart/<stream_name>', methods=['POST'])
    def monitor_restart_stream(stream_name):
        """Принудительный перезапуск потока через монитор"""
        if stream_name in stream_managers:
            manager = stream_managers[stream_name]
            if stream_name in monitor.health_stats:
                health = monitor.health_stats[stream_name]
                monitor._restart_stream(stream_name, manager, health)
                return jsonify({'success': True, 'message': f'Restarting {stream_name}'})
        return jsonify({'success': False, 'error': 'Stream not found'}), 404
    
    return monitor