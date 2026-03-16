#!/usr/bin/env python3
"""
Daily Reporter for OpenIPC Bridge
Ежедневные отчеты о состоянии камер в Telegram
"""

import os
import time
import threading
import logging
import schedule
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests

logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ ====================

REPORT_TIME = "21:00"  # Время отправки отчета (21:00)
MAX_FAILURES_HISTORY = 100  # Максимальное количество хранимых сбоев


# ==================== КЛАСС МЕНЕДЖЕРА ОТЧЕТОВ ====================

class DailyReporter:
    """Ежедневные отчеты о состоянии камер"""
    
    def __init__(self, monitor_manager, config_manager):
        self.monitor_manager = monitor_manager
        self.config_manager = config_manager
        self.running = True
        self.reports_history = []
        self.failures_history = []
        
        # Запускаем планировщик в отдельном потоке
        self._start_scheduler()
        
        logger.info(f"📊 DailyReporter initialized, reports scheduled at {REPORT_TIME}")
    
    def _start_scheduler(self):
        """Запустить планировщик отчетов"""
        def scheduler_loop():
            # Настраиваем ежедневный отчет
            schedule.every().day.at(REPORT_TIME).do(self._send_daily_report)
            
            # Также отправляем отчет сразу при запуске для теста
            # Можно закомментировать после отладки
            # threading.Timer(10, self._send_daily_report).start()
            
            while self.running:
                schedule.run_pending()
                time.sleep(60)
        
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
        logger.info("✅ Daily reporter scheduler started")
    
    def _send_daily_report(self):
        """Сформировать и отправить ежедневный отчет"""
        logger.info("📊 Generating daily report...")
        
        try:
            # Получаем статус всех мониторов
            status = self.monitor_manager.get_status()
            
            if not status['success']:
                logger.error("Failed to get monitor status for report")
                return
            
            monitors = status.get('monitors', {})
            
            # Собираем статистику
            total = len(monitors)
            healthy = 0
            warning = 0
            unhealthy = 0
            total_restarts = 0
            cameras_info = []
            
            for ip, mon in monitors.items():
                total_restarts += mon.get('restart_count', 0)
                
                camera_status = mon.get('status', 'unknown')
                if camera_status == 'healthy':
                    healthy += 1
                elif camera_status == 'warning':
                    warning += 1
                else:
                    unhealthy += 1
                
                # Собираем детальную информацию
                last_check = mon.get('last_check', 'unknown')
                failures = mon.get('failures', 0)
                ssh = "✅" if mon.get('ssh_available') else "❌"
                
                cameras_info.append({
                    'ip': ip,
                    'status': camera_status,
                    'failures': failures,
                    'ssh': ssh,
                    'restarts': mon.get('restart_count', 0),
                    'last_check': last_check
                })
            
            # Получаем статистику по записям
            recordings_stats = self._get_recordings_stats()
            
            # Получаем статистику по снимкам
            snapshots_stats = self._get_snapshots_stats()
            
            # Формируем отчет
            report = self._format_report(
                total, healthy, warning, unhealthy, 
                total_restarts, cameras_info,
                recordings_stats, snapshots_stats
            )
            
            # Отправляем в Telegram
            success = self._send_telegram_report(report)
            
            if success:
                # Сохраняем в историю
                self.reports_history.append({
                    'time': datetime.now().isoformat(),
                    'report': report,
                    'stats': {
                        'total': total,
                        'healthy': healthy,
                        'warning': warning,
                        'unhealthy': unhealthy,
                        'restarts': total_restarts
                    }
                })
                
                # Очищаем историю сбоев за день
                self.failures_history = []
                
                logger.info(f"✅ Daily report sent: {healthy}/{total} healthy")
            else:
                logger.error("❌ Failed to send daily report")
                
        except Exception as e:
            logger.error(f"❌ Error generating daily report: {e}")
            logger.exception(e)
    
    def _get_recordings_stats(self) -> dict:
        """Получить статистику по записям"""
        try:
            # Пробуем получить через API
            import requests
            response = requests.get("http://localhost:5000/api/recordings/stats", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    return {
                        'total': data.get('total_recordings', 0),
                        'size_mb': data.get('total_size', 0) / (1024 * 1024),
                        'active': data.get('active_recordings', 0)
                    }
        except Exception as e:
            logger.debug(f"Could not get recordings stats: {e}")
        
        return {'total': 0, 'size_mb': 0, 'active': 0}
    
    def _get_snapshots_stats(self) -> dict:
        """Получить статистику по снимкам"""
        try:
            # Пробуем получить через API
            import requests
            response = requests.get("http://localhost:5000/api/snapshots/stats", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    stats = data.get('stats', {})
                    return {
                        'total': stats.get('total_count', 0),
                        'size_mb': stats.get('total_size_mb', 0)
                    }
        except Exception as e:
            logger.debug(f"Could not get snapshots stats: {e}")
        
        return {'total': 0, 'size_mb': 0}
    
    def _format_report(self, total: int, healthy: int, warning: int, unhealthy: int,
                      total_restarts: int, cameras_info: List[dict],
                      recordings_stats: dict, snapshots_stats: dict) -> str:
        """Форматировать отчет для отправки"""
        
        # Заголовок
        report = f"📊 *Ежедневный отчет OpenIPC Bridge*\n"
        report += f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        report += "═" * 30 + "\n\n"
        
        # Общая статистика
        report += "📈 *Общая статистика:*\n"
        report += f"• Всего камер: *{total}*\n"
        report += f"• ✅ Здоровых: *{healthy}*\n"
        report += f"• ⚠️ С предупреждениями: *{warning}*\n"
        report += f"• ❌ Проблемных: *{unhealthy}*\n"
        report += f"• 🔄 Перезапусков majestic: *{total_restarts}*\n\n"
        
        # Статистика записей
        report += "🎥 *Записи:*\n"
        report += f"• Всего записей: *{recordings_stats['total']}*\n"
        report += f"• Общий размер: *{recordings_stats['size_mb']:.1f} MB*\n"
        report += f"• Активных записей: *{recordings_stats['active']}*\n\n"
        
        # Статистика снимков
        report += "📸 *Снимки:*\n"
        report += f"• Всего снимков: *{snapshots_stats['total']}*\n"
        report += f"• Общий размер: *{snapshots_stats['size_mb']:.1f} MB*\n\n"
        
        # Детальная информация по камерам
        report += "📋 *Состояние камер:*\n"
        
        # Сортируем: сначала проблемные, потом с предупреждениями, потом здоровые
        sorted_cameras = sorted(cameras_info, 
                               key=lambda x: (0 if x['status'] == 'unhealthy' else 
                                            1 if x['status'] == 'warning' else 2))
        
        for cam in sorted_cameras:
            status_icon = "✅" if cam['status'] == 'healthy' else "⚠️" if cam['status'] == 'warning' else "❌"
            ssh_icon = cam['ssh']
            
            report += f"{status_icon} `{cam['ip']}` "
            report += f"| Сбои: {cam['failures']}/3 "
            report += f"| SSH: {ssh_icon} "
            report += f"| Рестарты: {cam['restarts']}\n"
        
        # Добавляем историю сбоев за день
        if self.failures_history:
            report += "\n⚠️ *История сбоев за день:*\n"
            for failure in self.failures_history[-10:]:  # показываем последние 10
                report += f"• {failure['time']}: {failure['camera']} - {failure['error']}\n"
        
        report += "\n" + "═" * 30
        report += "\n🔧 OpenIPC Bridge Monitoring"
        
        return report
    
    def _send_telegram_report(self, report: str) -> bool:
        """Отправить отчет в Telegram"""
        try:
            # Получаем настройки Telegram из конфига
            telegram_config = self.config_manager.config.get('telegram', {})
            bot_token = telegram_config.get('bot_token')
            chat_id = telegram_config.get('chat_id')
            
            if not bot_token or not chat_id:
                logger.warning("Telegram not configured, skipping report")
                return False
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                'chat_id': str(chat_id).strip(),
                'text': report,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            return result.get('ok', False)
            
        except Exception as e:
            logger.error(f"Failed to send Telegram report: {e}")
            return False
    
    def add_failure(self, camera_ip: str, error: str):
        """Добавить запись о сбое в историю"""
        self.failures_history.append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'camera': camera_ip,
            'error': error
        })
        
        # Ограничиваем размер истории
        if len(self.failures_history) > MAX_FAILURES_HISTORY:
            self.failures_history = self.failures_history[-MAX_FAILURES_HISTORY:]
    
    def get_reports_history(self, limit: int = 10) -> List[dict]:
        """Получить историю отчетов"""
        return self.reports_history[-limit:]
    
    def stop(self):
        """Остановить планировщик"""
        self.running = False


# ==================== ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР ====================

_reporter = None

def get_reporter(monitor_manager=None, config_manager=None) -> DailyReporter:
    """Получить глобальный экземпляр репортера"""
    global _reporter
    if _reporter is None and monitor_manager and config_manager:
        _reporter = DailyReporter(monitor_manager, config_manager)
    return _reporter