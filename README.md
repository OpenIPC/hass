# OpenIPC Camera for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/himik19872/openipc-hass)](https://github.com/himik19872/openipc-hass/releases)
[![License](https://img.shields.io/github/license/himik19872/openipc-hass)](LICENSE)

Интеграция для управления камерами на прошивке OpenIPC в Home Assistant.

## 📸 Скриншоты

| Управление камерой | Запись с OSD | Настройки |
|--------------------|--------------|-----------|
| ![Снимок экрана 2026-02-22 130328](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130328.png) | ![Снимок экрана 2026-02-22 130357](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130357.png) | ![Снимок экрана 2026-02-22 130412](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130412.png) |

| Датчики | Медиа-плеер | Автоматизации |
|---------|-------------|---------------|
| ![Снимок экрана 2026-02-22 130426](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130426.png) | ![Снимок экрана 2026-02-22 130505](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130505.png) | ![Снимок экрана 2026-02-22 130520](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130520.png) |

![Снимок экрана 2026-02-22 130554](https://github.com/himik19872/openipc-hass/blob/main/Снимок%20экрана%202026-02-22%20130554.png)
### OpenIPC Ecosystem for Home Assistant

This repository contains everything you need to integrate your **OpenIPC**, **Beward**, and **Vivotek** cameras into Home Assistant. It includes a custom integration and a powerful addon (OpenIPC Bridge) for advanced features like **QR code scanning** and **Text-to-Speech (TTS)**.

### ✨ Features

*   **📹 Video Surveillance:** RTSP streams, snapshots, recording to HA media.
*   **📊 Monitoring:** CPU temp, FPS, bitrate, SD card status, network stats.
*   **🚨 Events:** Motion detection, door status (Beward), LNPR (Beward).
*   **🔊 Text-to-Speech (TTS):** Voice notifications via camera speaker.
    *   **Beward DS07P-LP:** A-law format.
    *   **OpenIPC:** PCM format.
*   **📱 Notifications:** Telegram with photos and videos.
*   **➕ NEW: QR Code Scanner!** Scan QR codes via the OpenIPC Bridge addon to trigger automations (e.g., open a gate, grant access).
*   **🔄 Blueprint:** Ready-to-use automation blueprint for QR scanning.

### 📦 Installation

#### 1. OpenIPC Bridge Addon (Required for QR & TTS)

This addon handles QR scanning and TTS generation, removing the need for complex `shell_command` scripts.

1.  Add this repository to your Supervisor add-on store:
    *   Go to **Settings → Add-ons → Add-on store → ⋮ → Repositories**.
    *   Add: `https://github.com/OpenIPC/hass`
2.  Refresh the page. You'll find the new **OpenIPC Bridge** addon.
3.  Install and start the addon. It will be available at `http://[your-ha-ip]:5000`.
4.  **Configure** your cameras via the addon's **Web UI** (`http://[your-ha-ip]:5000/config`). Add your camera IPs, usernames, passwords, and endpoints.

#### 2. OpenIPC Integration

You can install the integration via HACS or manually.

##### HACS (Recommended)
1.  Open HACS.
2.  Go to **Integrations** and click the three dots in the top right → **Custom repositories**.
3.  Add `https://github.com/OpenIPC/hass` with category **Integration**.
4.  Click **Add** and then search for "OpenIPC Camera" in HACS to install.
5.  **Restart** Home Assistant.

##### Manual
1.  Download the latest release.
2.  Copy the `openipc` folder from `custom_components` to your `/config/custom_components/` directory.
3.  **Restart** Home Assistant.

### ⚙️ Configuration

#### Adding a Camera via UI
1.  Go to **Settings → Devices & Services**.
2.  Click **"Add Integration"** and search for **"OpenIPC Camera"**.
3.  Fill in your camera details. **Crucially, select the correct `Device Type`** (OpenIPC, Beward, Vivotek).

#### Setting up Telegram (Optional)
Add to your `configuration.yaml`:
```yaml
openipc:
  telegram_bot_token: "YOUR_BOT_TOKEN"
  telegram_chat_id: "YOUR_CHAT_ID"
🤖 Blueprint: QR Code Scanner
This blueprint creates an automation that starts QR scanning on a button press, checks the code, and performs actions like TTS, relay toggling, and Telegram notifications.

How to use:

Go to Settings → Automations → Blueprints.

Click "Import Blueprint" and paste the raw URL to the blueprint file in this repo: https://github.com/OpenIPC/hass/blob/main/blueprints/automation/openipc/qr_scanner.yaml

Click "Preview" and then "Create Automation".

Fill in the required entities:

Camera (e.g., camera.openipc_sip)

Media Player (e.g., media_player.openipc_sip_speaker)

Relay (optional, e.g., switch.nspanel_relay_1)

Expected QR Code

Trigger Entity (an input_boolean helper you create to start the scan)

Telegram options.

📝 Example Automations
Simple TTS Notification
yaml
alias: "Say Hello on Motion"
trigger:
  - platform: state
    entity_id: binary_sensor.openipc_sip_motion
    to: "on"
action:
  - service: media_player.play_media
    target:
      entity_id: media_player.openipc_sip_speaker
    data:
      media_content_id: "Hello, you are on camera!"
      media_content_type: "tts"
QR Scan for Gate Control
(The blueprint above does this automatically. Here's the core service call)

yaml
service: openipc.start_qr_scan
data:
  entity_id: camera.openipc_sip
  expected_code: "my_secret_gate_code"
  timeout: 60
🆘 Support
Check Home Assistant logs.

Check the addon's logs in Supervisor.

Verify camera connectivity (ping).

For TTS issues, check the addon's debug audio files in /config/www/.

🤝 Contributing
Pull requests are welcome!

📜 License
MIT
интеграция для управления IP-камерами на базе прошивок **OpenIPC**, **Beward** и **Vivotek** в Home Assistant. Поддерживает видеонаблюдение, запись, уведомления в Telegram, голосовые оповещения (TTS) и распознавание автомобильных номеров (LNPR) для Beward DS07P-LP.

## ✨ Возможности

### 📹 **Видеонаблюдение**
- Просмотр RTSP-потока (main/sub)
- Получение снимков с камеры
- Запись видео в медиа-папку HA
- OSD (наложение текста на видео)

### 📊 **Мониторинг**
- Температура CPU
- FPS, битрейт, разрешение
- Время работы (uptime)
- Состояние microSD-карты
- Сетевая статистика

### 🚨 **События**
- Детекция движения
- Состояние двери (для Beward)
- Тревожные события
- **LNPR** - распознавание автономеров (Beward DS07P-LP)

### 🔊 **Голосовые оповещения (TTS)**
- **Beward DS07P-LP** - через встроенный динамик (формат A-law)
- **OpenIPC камеры** - через подключенный динамик (формат PCM)
- Поддержка русского и английского языков
- Индивидуальные и групповые оповещения

### 📱 **Интеграции**
- Telegram (отправка видео и снимков)
- Автоматизации на все события
- Поддержка нескольких камер

---

OpenIPC Экосистема для Home Assistant
Этот репозиторий содержит всё необходимое для интеграции ваших камер OpenIPC, Beward и Vivotek в Home Assistant. Включает пользовательскую интеграцию и мощный аддон (OpenIPC Bridge) для расширенных функций, таких как сканирование QR-кодов и Text-to-Speech (TTS).

✨ Возможности
📹 Видеонаблюдение: RTSP-потоки, снимки, запись в медиа-папку HA.

📊 Мониторинг: Температура CPU, FPS, битрейт, статус SD-карты, сетевая статистика.

🚨 События: Детекция движения, состояние двери (Beward), распознавание номеров (LNPR) для Beward.

🔊 Голосовые оповещения (TTS): Через динамик камеры.

Beward DS07P-LP: Формат A-law.

OpenIPC: Формат PCM.

📱 Уведомления: Telegram с фото и видео.

➕ НОВИНКА: Сканер QR-кодов! Сканируйте QR-коды через аддон OpenIPC Bridge для запуска автоматизаций (например, открыть ворота, предоставить доступ).

🔄 Блюпринт: Готовая автоматизация для сканирования QR-кодов.

📦 Установка
1. Аддон OpenIPC Bridge (Необходим для QR и TTS)
Этот аддон обрабатывает сканирование QR и генерацию TTS, избавляя от необходимости в сложных скриптах shell_command.

Добавьте этот репозиторий в магазин аддонов Supervisor:

Перейдите в Настройки → Аддоны → Магазин аддонов → ⋮ → Репозитории.

Добавьте: https://github.com/OpenIPC/hass

Обновите страницу. Вы увидите новый аддон OpenIPC Bridge.

Установите и запустите аддон. Он будет доступен по адресу http://[IP-адрес-вашего-HA]:5000.

Настройте свои камеры через Веб-интерфейс аддона (http://[IP-адрес-вашего-HA]:5000/config). Добавьте IP-адреса камер, имена пользователей, пароли и эндпоинты.

2. Интеграция OpenIPC
Интеграцию можно установить через HACS или вручную.

HACS (Рекомендуется)
Откройте HACS.

Перейдите в Интеграции и нажмите три точки в правом верхнем углу → Пользовательские репозитории.

Добавьте https://github.com/OpenIPC/hass с категорией Интеграция.

Нажмите "Добавить", затем найдите "OpenIPC Camera" в HACS и установите.

Перезапустите Home Assistant.

Вручную
Скачайте последний релиз.

Скопируйте папку openipc из custom_components в вашу директорию /config/custom_components/.

Перезапустите Home Assistant.

⚙️ Настройка
Добавление камеры через UI
Перейдите в Настройки → Устройства и службы.

Нажмите "Добавить интеграцию" и найдите "OpenIPC Camera".

Заполните данные камеры. Крайне важно выбрать правильный Тип устройства (OpenIPC, Beward, Vivotek).

Настройка Telegram (Опционально)
Добавьте в ваш configuration.yaml:

yaml
openipc:
  telegram_bot_token: "ВАШ_ТОКЕН_БОТА"
  telegram_chat_id: "ВАШ_CHAT_ID"
🤖 Блюпринт: Сканер QR-кодов
Этот блюпринт создает автоматизацию, которая запускает сканирование по нажатию кнопки, проверяет код и выполняет действия: TTS, управление реле, уведомления в Telegram.

Как использовать:

Перейдите в Настройки → Автоматизации → Сценарии (Blueprints).

Нажмите "Импортировать сценарий" и вставьте сырую ссылку на файл блюпринта в этом репозитории: https://github.com/OpenIPC/hass/blob/main/blueprints/automation/openipc/qr_scanner.yaml

Нажмите "Предпросмотр", затем "Создать автоматизацию".

Заполните необходимые поля:

Камера (например, camera.openipc_sip)

Медиа-плеер (например, media_player.openipc_sip_speaker)

Реле (опционально, например, switch.nspanel_relay_1)

Ожидаемый QR код

Сущность-триггер (вспомогательный input_boolean, который вы создадите для запуска сканирования)

Настройки Telegram.

📝 Примеры автоматизаций
Простое TTS-уведомление
yaml
alias: "Сказать 'Привет' при движении"
trigger:
  - platform: state
    entity_id: binary_sensor.openipc_sip_motion
    to: "on"
action:
  - service: media_player.play_media
    target:
      entity_id: media_player.openipc_sip_speaker
    data:
      media_content_id: "Привет, вы в кадре!"
      media_content_type: "tts"
Сканирование QR для управления воротами
(Блюпринт выше делает это автоматически. Здесь показан основной вызов сервиса)

yaml
service: openipc.start_qr_scan
data:
  entity_id: camera.openipc_sip
  expected_code: "мой_секретный_код_ворот"
  timeout: 60
🆘 Поддержка
Проверьте логи Home Assistant.

Проверьте логи аддона в Supervisor.

Убедитесь, что камера доступна (ping).

При проблемах с TTS проверьте отладочные аудиофайлы аддона в /config/www/.

🤝 Вклад в проект
Мы приветствуем ваши пул-реквесты!

📜 Лицензия
MIT













