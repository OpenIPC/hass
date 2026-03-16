#!/usr/bin/env python3
"""
Camera Monitor for OpenIPC Bridge
Мониторинг камер на прошивке OpenIPC и автоматический перезапуск majestic
"""

import os
import time
import threading
import logging
import paramiko
import socket
import requests
from datetime import datetime
from typing import Dict, Optional, List
from collections import deque

logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ ====================

CHECK_INTERVAL = 60  # Проверка каждые 60 секунд
RTSP_TIMEOUT = 5     # Таймаут для проверки RTSP
HTTP_TIMEOUT = 3     # Таймаут для проверки HTTP
METRICS_TIMEOUT = 3  # Таймаут для проверки метрик
SSH_PORT = 22        # Порт SSH
SSH_TIMEOUT = 10     # Таймаут SSH соединения
MAX_FAILURES = 3     # Количество неудачных проверок до перезапуска

# Команды для перезапуска majestic
RESTART_COMMANDS = [
    "killall majestic 2>/dev/null",
    "majestic >/dev/null 2>&1 &",
    "sleep 2",
    "pidof majestic || (echo 'Failed to start majestic' && exit 1)"
]

# Команды для проверки статуса
STATUS_COMMANDS = {
    'pid': "pidof majestic || echo 'not running'",
    'memory': "ps aux | grep majestic | grep -v grep | awk '{print $6}' || echo '0'",
    'cpu': "ps aux | grep majestic | grep -v grep | awk '{print $3}' || echo '0'",
    'uptime': "ps -o etimes= -C majestic 2>/dev/null || echo '0'"
}


# ==================== КЛАСС МОНИТОРА КАМЕРЫ ====================

class CameraMonitor(threading.Thread):
    """Мониторинг отдельной камеры на прошивке OpenIPC"""
    
    def __init__(self, camera_ip: str, username: str = 'root', password: str = '12345', 
                 rtsp_port: int = 554, http_port: int = 80, metrics_port: int = 80):
        super().__init__()
        self.camera_ip = camera_ip
        self.username = username
        self.password = password
        self.rtsp_port = rtsp_port
        self.http_port = http_port
        self.metrics_port = metrics_port
        self.metrics_path = "/metrics"
        self.name = f"Camera-{camera_ip}"
        
        # Статистика
        self.failures = 0
        self.restart_count = 0
        self.last_check = None
        self.last_restart = None
        self.last_success = None
        self.status = "unknown"
        self.error_history = deque(maxlen=10)
        
        # Метрики
        self.last_metrics = {}
        self.metrics_history = deque(maxlen=60)  # последний час проверок
        
        # Флаги
        self.running = True
        self.ssh_available = False
        
        # Репортер (будет установлен извне)
        self.reporter = None
        
        logger.info(f"📹 CameraMonitor initialized for {self.camera_ip}")
    
    def set_reporter(self, reporter):
        """Установить репортер для отправки уведомлений"""
        self.reporter = reporter
    
    def run(self):
        """Основной цикл мониторинга"""
        logger.info(f"🚀 Starting monitor for {self.camera_ip}")
        
        while self.running:
            try:
                self._check_camera()
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in monitor loop for {self.camera_ip}: {e}")
                time.sleep(CHECK_INTERVAL)
        
        logger.info(f"🛑 Monitor stopped for {self.camera_ip}")
    
    def _check_camera(self):
        """Проверить состояние камеры"""
        self.last_check = datetime.now()
        
        # Проверка 1: RTSP доступность
        rtsp_ok = self._check_rtsp()
        
        # Проверка 2: HTTP доступность
        http_ok = self._check_http()
        
        # Проверка 3: Метрики majestic
        metrics_ok, metrics = self._check_metrics()
        
        # Проверка 4: SSH доступность
        ssh_ok = self._check_ssh()
        
        # Проверка 5: Статус процесса majestic (через SSH)
        majestic_running = False
        majestic_info = {}
        if ssh_ok:
            majestic_running, majestic_info = self._check_majestic_status()
        
        # Сохраняем метрики
        if metrics_ok:
            self.last_metrics = metrics
            self.metrics_history.append({
                'time': datetime.now().isoformat(),
                'metrics': metrics
            })
        
        # Анализ результатов
        if rtsp_ok and metrics_ok:
            # RTSP и метрики работают - камера полностью здорова
            self.failures = 0
            self.status = "healthy"
            self.last_success = datetime.now()
            logger.debug(f"✅ {self.camera_ip} is healthy (RTSP: ✅, Metrics: ✅)")
        elif rtsp_ok:
            # RTSP работает, но метрики нет - возможно проблемы с веб-интерфейсом
            self.failures += 0.5  # половинная ошибка
            self.status = "warning"
            error_msg = f"RTSP: ✅, Metrics: ❌, HTTP: {'✅' if http_ok else '❌'}"
            self.error_history.append({
                'time': datetime.now().isoformat(),
                'error': error_msg
            })
            
            # Добавляем в историю сбоев репортера
            if self.reporter:
                self.reporter.add_failure(self.camera_ip, error_msg)
            
            logger.warning(f"⚠️ {self.camera_ip} has warning: {error_msg}")
        else:
            # RTSP не работает - серьезная проблема
            self.failures += 1
            error_msg = f"RTSP: {'✅' if rtsp_ok else '❌'}, Metrics: {'✅' if metrics_ok else '❌'}, Majestic: {'✅' if majestic_running else '❌'}"
            self.error_history.append({
                'time': datetime.now().isoformat(),
                'error': error_msg
            })
            
            # Добавляем в историю сбоев репортера
            if self.reporter:
                self.reporter.add_failure(self.camera_ip, error_msg)
            
            logger.warning(f"⚠️ {self.camera_ip} check failed ({self.failures}/{MAX_FAILURES}): {error_msg}")
            
            # Если превышен лимит неудач, пробуем перезапустить
            if self.failures >= MAX_FAILURES:
                self._restart_majestic()
    
    def _check_http(self) -> bool:
        """Проверить доступность HTTP веб-интерфейса"""
        try:
            url = f"http://{self.camera_ip}:{self.http_port}"
            response = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            return response.status_code in [200, 401, 403, 404, 500]
        except Exception as e:
            logger.debug(f"HTTP check error for {self.camera_ip}: {e}")
            return False
    
    def _check_rtsp(self) -> bool:
        """Проверить доступность RTSP порта"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(RTSP_TIMEOUT)
            result = sock.connect_ex((self.camera_ip, self.rtsp_port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"RTSP check error for {self.camera_ip}: {e}")
            return False
    
    def _check_metrics(self) -> tuple:
        """Проверить метрики majestic"""
        try:
            url = f"http://{self.camera_ip}:{self.metrics_port}{self.metrics_path}"
            response = requests.get(url, timeout=METRICS_TIMEOUT)
            
            if response.status_code != 200:
                return False, {}
            
            # Парсим метрики
            metrics = self._parse_metrics(response.text)
            
            # Проверяем критичные метрики
            if 'node_hwmon_temp_celsius' in metrics:
                temp = metrics['node_hwmon_temp_celsius']
                if temp > 80:  # Слишком высокая температура
                    logger.warning(f"⚠️ {self.camera_ip} high temperature: {temp}°C")
            
            if 'isp_fps' in metrics:
                fps = metrics['isp_fps']
                if fps < 1:  # Слишком низкий FPS
                    logger.warning(f"⚠️ {self.camera_ip} low FPS: {fps}")
            
            if 'node_memory_MemAvailable_bytes' in metrics:
                mem_available = metrics['node_memory_MemAvailable_bytes'] / 1024 / 1024
                mem_total = metrics.get('node_memory_MemTotal_bytes', 0) / 1024 / 1024
                if mem_available < 10:  # Меньше 10MB свободно
                    logger.warning(f"⚠️ {self.camera_ip} low memory: {mem_available:.1f}MB free of {mem_total:.1f}MB")
            
            return True, metrics
            
        except Exception as e:
            logger.debug(f"Metrics check error for {self.camera_ip}: {e}")
            return False, {}
    
    def _parse_metrics(self, text: str) -> dict:
        """Парсинг Prometheus метрик"""
        metrics = {}
        lines = text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Простые метрики без лейблов
            parts = line.split()
            if len(parts) >= 2:
                try:
                    name = parts[0]
                    value = float(parts[1])
                    metrics[name] = value
                except (ValueError, IndexError):
                    continue
        
        return metrics
    
    def _check_ssh(self) -> bool:
        """Проверить доступность SSH"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((self.camera_ip, SSH_PORT))
            sock.close()
            self.ssh_available = (result == 0)
            return self.ssh_available
        except Exception as e:
            logger.debug(f"SSH check error for {self.camera_ip}: {e}")
            self.ssh_available = False
            return False
    
    def _check_majestic_status(self) -> tuple:
        """Проверить статус процесса majestic через SSH"""
        if not self.ssh_available:
            return False, {}
        
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.camera_ip,
                port=SSH_PORT,
                username=self.username,
                password=self.password,
                timeout=SSH_TIMEOUT,
                allow_agent=False,
                look_for_keys=False
            )
            
            # Получаем PID
            stdin, stdout, stderr = client.exec_command(STATUS_COMMANDS['pid'])
            pid_output = stdout.read().decode().strip()
            majestic_running = pid_output and pid_output != 'not running'
            
            # Получаем дополнительную информацию
            info = {}
            if majestic_running:
                for key, cmd in STATUS_COMMANDS.items():
                    if key != 'pid':
                        stdin, stdout, stderr = client.exec_command(cmd)
                        info[key] = stdout.read().decode().strip()
            
            client.close()
            return majestic_running, info
            
        except Exception as e:
            logger.error(f"SSH error for {self.camera_ip}: {e}")
            self.ssh_available = False
            return False, {}
    
    def _restart_majestic(self):
        """Перезапустить majestic через SSH"""
        logger.warning(f"🔄 Attempting to restart majestic on {self.camera_ip}")
        
        if not self.ssh_available:
            logger.error(f"❌ Cannot restart {self.camera_ip} - SSH not available")
            self.failures = 0
            return
        
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.camera_ip,
                port=SSH_PORT,
                username=self.username,
                password=self.password,
                timeout=SSH_TIMEOUT,
                allow_agent=False,
                look_for_keys=False
            )
            
            for cmd in RESTART_COMMANDS:
                logger.debug(f"Executing: {cmd}")
                stdin, stdout, stderr = client.exec_command(cmd)
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0 and 'exit 1' not in cmd:
                    error = stderr.read().decode().strip()
                    logger.error(f"Command failed (exit {exit_status}): {error}")
            
            client.close()
            
            self.restart_count += 1
            self.last_restart = datetime.now()
            self.failures = 0
            logger.info(f"✅ Majestic restarted on {self.camera_ip}")
            
        except Exception as e:
            logger.error(f"❌ Failed to restart majestic on {self.camera_ip}: {e}")
    
    def stop(self):
        """Остановить мониторинг"""
        self.running = False
    
    def get_status(self) -> dict:
        """Получить текущий статус камеры"""
        return {
            'ip': self.camera_ip,
            'name': self.name,
            'status': self.status,
            'failures': self.failures,
            'restart_count': self.restart_count,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'last_restart': self.last_restart.isoformat() if self.last_restart else None,
            'last_success': self.last_success.isoformat() if self.last_success else None,
            'ssh_available': self.ssh_available,
            'error_history': list(self.error_history),
            'last_metrics': self.last_metrics
        }


# ==================== МЕНЕДЖЕР МОНИТОРОВ ====================

class MonitorManager:
    """Управление мониторами для всех камер"""
    
    def __init__(self):
        self.monitors: Dict[str, CameraMonitor] = {}
        self.lock = threading.Lock()
        self.running = True
        self.reporter = None
        self._start_cleanup_thread()
    
    def set_reporter(self, reporter):
        """Установить репортер для всех мониторов"""
        self.reporter = reporter
        with self.lock:
            for monitor in self.monitors.values():
                monitor.set_reporter(reporter)
    
    def _start_cleanup_thread(self):
        """Запустить поток для очистки остановленных мониторов"""
        def cleanup_loop():
            while self.running:
                time.sleep(60)
                self._cleanup_stopped_monitors()
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()
    
    def _cleanup_stopped_monitors(self):
        """Удалить остановленные мониторы"""
        with self.lock:
            to_remove = []
            for ip, monitor in self.monitors.items():
                if not monitor.running:
                    to_remove.append(ip)
            
            for ip in to_remove:
                logger.info(f"Removing stopped monitor for {ip}")
                del self.monitors[ip]
    
    def add_camera(self, camera_ip: str, username: str = 'root', 
                   password: str = '12345', rtsp_port: int = 554, http_port: int = 80):
        """Добавить камеру в мониторинг"""
        with self.lock:
            if camera_ip in self.monitors:
                logger.warning(f"Monitor already exists for {camera_ip}")
                return False
            
            monitor = CameraMonitor(camera_ip, username, password, rtsp_port, http_port)
            if self.reporter:
                monitor.set_reporter(self.reporter)
            monitor.start()
            self.monitors[camera_ip] = monitor
            logger.info(f"✅ Added monitor for {camera_ip}")
            return True
    
    def remove_camera(self, camera_ip: str):
        """Удалить камеру из мониторинга"""
        with self.lock:
            if camera_ip in self.monitors:
                self.monitors[camera_ip].stop()
                logger.info(f"Stopping monitor for {camera_ip}")
                return True
            return False
    
    def get_status(self, camera_ip: str = None) -> dict:
        """Получить статус мониторов"""
        if camera_ip:
            if camera_ip in self.monitors:
                return {'success': True, 'monitor': self.monitors[camera_ip].get_status()}
            return {'success': False, 'error': 'Monitor not found'}
        
        statuses = {}
        with self.lock:
            for ip, monitor in self.monitors.items():
                statuses[ip] = monitor.get_status()
        
        return {
            'success': True,
            'total': len(self.monitors),
            'monitors': statuses
        }
    
    def restart_camera(self, camera_ip: str) -> dict:
        """Принудительно перезапустить камеру"""
        if camera_ip in self.monitors:
            self.monitors[camera_ip]._restart_majestic()
            return {'success': True, 'message': f'Restart initiated for {camera_ip}'}
        return {'success': False, 'error': 'Monitor not found'}
    
    def stop_all(self):
        """Остановить все мониторы"""
        self.running = False
        with self.lock:
            for monitor in self.monitors.values():
                monitor.stop()


# ==================== ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР ====================

_monitor_manager = None

def get_monitor_manager() -> MonitorManager:
    """Получить глобальный экземпляр менеджера мониторов"""
    global _monitor_manager
    if _monitor_manager is None:
        _monitor_manager = MonitorManager()
    return _monitor_manager