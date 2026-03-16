
### OpenIPC Ecosystem for Home Assistant

Integration for managing OpenIPC, Beward, and Vivotek cameras in Home Assistant with a powerful web interface for advanced features.

## ✨ Features

### 📹 Video Surveillance
- RTSP streams and snapshots
- Recording to HA media folder with OSD overlay
- PTZ control for Vivotek
- Relay control for Beward

### 📊 Monitoring
- CPU temperature, FPS, bitrate
- SD card status, network statistics
- License plate recognition (LNPR) for Beward

### 🔊 Text-to-Speech (TTS)
- **Google TTS** - cloud-based speech synthesis
- **RHVoice** - local synthesis (Anna voice) via separate addon
- Support for Beward (A-law) and OpenIPC (PCM)

### 📱 Notifications
- Send photos and videos to Telegram
- Visual notification builder

### ➕ NEW (March 2026)

#### 🖥️ **OpenIPC Bridge Addon** with Web UI
- Camera management through beautiful interface
- Import cameras from OpenIPC integration
- OSD configuration with drag-and-drop preview
- QR code generator with Telegram integration
- TTS provider selection (Google/RHVoice)

#### 🎨 **Visual OSD Editor**
- Drag-and-drop regions with mouse
- Real-time preview
- Save and load templates
- Logo support (BMP)

#### 📸 **QR Scanner & Generator**
- Continuous QR code scanning
- Customizable QR code generation
- Scan history with CSV export
- Send QR codes to Telegram

#### 🔄 **Universal Blueprint**
- Video recording on door opening
- Dynamic OSD with ticking clock
- TTS provider selection
- Telegram integration

---

## 📦 Installation

### 1. OpenIPC Bridge Addon (Required for new features)

```bash
# Add the repository to Supervisor:
# Settings → Add-ons → Add-on Store → ⋮ → Repositories
# Add: https://github.com/OpenIPC/hass

After installation, the addon will be available at http://[YOUR-HA-IP]:5000

2. OpenIPC Integration
Via HACS (recommended)
Open HACS → Integrations → ⋮ → Custom repositories

Add https://github.com/OpenIPC/hass with category Integration

Find "OpenIPC Camera" and install

Restart HA

Manual
Copy the custom_components/openipc folder to /config/custom_components/ and restart HA.


🎮 Using the Addon Web Interface
After installation, open http://[YOUR-HA-IP]:5000. You'll see the main dashboard.

📹 "Cameras" Tab
Import from Home Assistant
Click "Import cameras from HA" - the addon will automatically pull all cameras from the OpenIPC integration with correct settings based on type:

🟦 OpenIPC - standard IP cameras

🟩 Beward - doorbells with relays

🟨 Vivotek - PTZ cameras

Manual Addition
If you need to add a camera manually, fill out the form:

Name - friendly name

IP Address - e.g., 192.168.1.4

Type - OpenIPC/Beward/Vivotek

Username/Password - access credentials

🖥️ "OSD" Tab (On-Screen Display)
Visual editor for overlaying text on video:

Select a camera from the list

Configure regions (up to 4):

Region 0 - for logo (BMP)

Regions 1-3 - for text

Drag with mouse - position updates in real-time

Customize appearance:

Text color (RGB picker)

Font size (8-72 px)

Font (Ubuntu Mono, Arial, Times)

Opacity (0-255)

Use variables:

$t - current time (ticks in real-time!)

$B - bitrate

$C - frame counter

$M - memory usage

Save templates for quick application

OSD Examples:

Region 1: "🚪 DOOR OPEN! 03/13/2026 15:23:45" (red, 48px)
Region 2: "⏺️ RECORDING: 3 MIN" (yellow, 36px)
Region 3: "⏱️ 15:23:45" (green, 32px) - ticking clock

📸 "QR Scanner & Generator" Tab
Scanner
Select a camera

Configure expected code (optional)

Click "Start scanning"

When QR code is detected, HA generates openipc_qr_detected event

Generator
Enter text or URL

Adjust size, colors, error correction level

Click "Generate"

"Save to file" or "Send to Telegram"

History
All scans are saved

CSV export

Copy code to clipboard

Quick QR generation from history

🔊 "TTS" Tab (Text-to-Speech)
Voice notification settings with support for different providers:

Available Providers:
Google TTS - cloud-based, 30+ languages, high quality

RHVoice - local, offline, "Anna" voice (requires separate addon)

How to use:
Select camera

Choose provider

Select language

Enter text

Click "Test"

Verification:
Success - green notification

Debug files saved to /config/www/tts_debug_*.pcm

🤖 Blueprints
Blueprint 1: QR Scanner (existing in repository)


# Import URL:
https://github.com/OpenIPC/hass/blob/main/blueprints/automation/openipc/qr_scanner.yaml


Creates an automation that starts scanning on button press, checks the code, and performs actions:

TTS notification

Relay control

Telegram notifications

Blueprint 2: Door Opening Video Recording (NEW!)

# Import URL:
https://github.com/OpenIPC/hass/blob/main/blueprints/automation/openipc/door_recording.yaml

Universal automation with advanced features:

Settings:
Door sensor - any binary_sensor with device_class door

Camera - OpenIPC camera

Media player - camera speaker

TTS provider - Google or RHVoice

Recording duration - 10 to 600 seconds

OSD regions - select numbers for text

Telegram - enable/disable sending

Post-recording time - how many seconds to show ticking clock

What it does:
Clears old OSD

TTS: "Door open, starting recording" (selected provider)

Sets OSD with date and time (with ticking clock!)

Records video for specified duration

After recording shows ticking clock on screen

TTS: "Recording complete"

Sends video to Telegram (if enabled)

TTS: "Video sent to Telegram"

Clears screen

OSD during recording:

🚪 DOOR OPEN! 03/13/2026
⏺️ RECORDING: 3 MIN


OSD after recording:

03/13/2026
⏱️ 15:23:45  (ticks!)


📝 Automation Examples
Simple TTS Notification


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
      extra:
        provider: "rhvoice"  # or "google"


TTS with Dynamic Provider Selection


QR Scan for Gate Control

alias: "Open Gate with QR Code"
trigger:
  - platform: event
    event_type: openipc_qr_detected
condition:
  - condition: template
    value_template: "{{ trigger.event.data.data == 'secret_gate_code' }}"
action:
  - service: switch.turn_on
    entity_id: switch.gate_relay
  - delay:
      seconds: 1
  - service: switch.turn_off
    entity_id: switch.gate_relay
  - service: media_player.play_media
    target:
      entity_id: media_player.openipc_sip_speaker
    data:
      media_content_id: "Access granted, gate open"
      media_content_type: "tts"



🔧 Setting up RHVoice (Local TTS)
To use RHVoice, install a separate addon:

Add repository: https://github.com/definitio/ha-rhvoice-addon

Install RHVoice Home Assistant addon

Install RHVoice integration via HACS

In integration settings, set host: localhost

After this, RHVoice will be available in our blueprint and web interface.

📊 Project Structure



/
├── custom_components/openipc/     # HA Integration
│   ├── __init__.py
│   ├── api.py
│   ├── api_ha.py                  # API for addon
│   ├── beward_device.py
│   ├── binary_sensor.py
│   ├── button.py
│   ├── camera.py
│   ├── commands.py
│   ├── config_flow.py
│   ├── const.py
│   ├── coordinator.py
│   ├── diagnostics.py
│   ├── discovery.py
│   ├── helpers.py
│   ├── lnpr.py
│   ├── media_player.py
│   ├── migration.py
│   ├── notify.py
│   ├── onvif_client.py
│   ├── openipc_audio.py
│   ├── osd_manager.py
│   ├── parsers.py
│   ├── ptz.py
│   ├── ptz_entity.py
│   ├── qr_scanner.py
│   ├── qr_utils.py
│   ├── recorder.py
│   ├── recording.py
│   ├── select.py
│   ├── sensor.py
│   ├── services.py
│   ├── services_impl.py
│   ├── switch.py
│   ├── vivotek_device.py
│   ├── vivotek_ptz.py
│   └── vivotek_ptz_entities.py
│
├── addon/                          # OpenIPC Bridge Addon
│   ├── Dockerfile
│   ├── config.yaml
│   ├── run.sh
│   ├── server.py
│   ├── tts_generate_openipc.sh
│   ├── tts_generate.sh
│   ├── tts_generate_rhvoice.sh     # New script for RHVoice
│   ├── check_modules.py
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── config.html
│       ├── osd.html                # Visual OSD editor
│       ├── qr.html                 # QR scanner & generator
│       └── tts.html                # TTS with provider selection
│
└── blueprints/automation/openipc/
    ├── qr_scanner.yaml             # Existing blueprint
    └── door_recording.yaml         # NEW blueprint with TTS selection



🆘 Support & Troubleshooting
Logs
Integration: Settings → System → Logs → openipc

Addon: Supervisor → OpenIPC Bridge → Logs

TTS Debug: check /config/www/tts_debug_*.pcm

Common Issues
OSD not appearing
Check if OSD service is running on camera (ps | grep osd)

Verify port 9000 accessibility (netstat -tlnp | grep 9000)

In OSD web interface, check opacity settings (opacity: 255)

TTS not working
Verify camera accessibility (ping)

Check correct endpoint (/play_audio for OpenIPC)

For RHVoice: ensure separate addon is running

Camera import from HA not working
Verify http dependency in manifest.json

Check endpoint: http://[HA_IP]:8123/api/openipc/cameras
# OpenIPC Camera for Home Assistant 🚀





## ✨ Ключевые возможности / Key Features

### 📹 Видеонаблюдение / Video Surveillance
*   **RTSP потоки и снимки / RTSP streams and snapshots**
*   **Запись в медиа-папку HA с OSD / Recording to HA media folder with OSD**
*   PTZ управление для Vivotek / PTZ control for Vivotek
*   Управление реле для Beward / Relay control for Beward

### 📊 Мониторинг / Monitoring
*   Температура CPU, FPS, битрейт / CPU temperature, FPS, bitrate
*   Статус SD-карты, сетевая статистика / SD card status, network statistics
*   Распознавание номеров (LNPR) для Beward / License plate recognition (LNPR) for Beward

### 🔊 Text-to-Speech (TTS)
*   **Google TTS** (облачный / cloud-based)
*   **RHVoice** (локальный, голос "Анна" / local, "Anna" voice)
*   Поддержка Beward (A-law) и OpenIPC (PCM) / Support for Beward (A-law) and OpenIPC (PCM)

### 📱 Уведомления / Notifications
*   Отправка фото и видео в Telegram / Send photos and videos to Telegram
*   **Визуальный конструктор уведомлений / Visual notification builder**

---

## 🚀 Что нового 16 марта 2026 / What's New on March 16, 2026

Это крупнейшее обновление нашего аддона `openipc-bridge`, которое превращает его в полноценную систему видеонаблюдения. Мы переработали архитектуру, добавили запись видео, умный мониторинг камер и многое другое!
This is the biggest update to our `openipc-bridge` addon, turning it into a full-fledged video surveillance system. We've reworked the architecture, added video recording, smart camera monitoring, and much more!

### 🎥 **Новая система записи / New Recording System**
*   **Гибкие настройки:** Вы можете настроить качество записи (`high`/`medium`/`low`), FPS (5-30), и длительность сегмента (от 1 минуты до 1 часа) для каждой камеры индивидуально.
    **Flexible settings:** You can now configure recording quality (`high`/`medium`/`low`), FPS (5-30), and segment duration (from 1 minute to 1 hour) for each camera individually.
*   **Управление ресурсами:** Добавлен лимит на количество одновременных записей (по умолчанию 5), чтобы не перегружать систему, на которой запущен аддон. Вы можете легко изменить этот лимит в интерфейсе.
    **Resource management:** A limit on the number of simultaneous recordings has been added (default 5) to prevent overloading the system running the addon. You can easily change this limit in the UI.
*   **Автоматическая очистка:** Старые записи автоматически удаляются согласно заданной глубине архива (в днях).
    **Automatic cleanup:** Old recordings are automatically deleted according to the set archive depth (in days).
*   **Удобный архив:** Новая страница архива с мощными фильтрами (по нескольким камерам, дате и времени, типу событий), встроенным видеоплеером и временной шкалой с отметками событий.
    **Convenient archive:** A new archive page with powerful filters (by multiple cameras, date & time, event type), a built-in video player, and a timeline with event marks.

### 🔔 **Новые уведомления / New Notifications**
*   **Централизованные настройки Telegram:** Все настройки Telegram (токен, chat ID, качество видео) теперь собраны на отдельной странице "Уведомления".
    **Centralized Telegram settings:** All Telegram settings (token, chat ID, video quality) are now collected on a separate "Notifications" page.
*   **Отправка снимков и видео:** Вы можете отправлять снимки прямо из плеера архива, а также видео (с возможностью сжатия для экономии трафика).
    **Sending snapshots and videos:** You can send snapshots directly from the archive player, as well as videos (with the option to compress them to save traffic).

### 🩺 **Мониторинг и автоматическое восстановление / Monitoring & Self-Healing**
*   **Умный мониторинг majestic:** Аддон теперь самостоятельно следит за состоянием каждой OpenIPC камеры. Он проверяет не только ping, но и доступность RTSP-порта (554) и, что самое важное, анализирует метрики самого процесса `majestic` (температура, FPS, память), получаемые через `/metrics`.
    **Smart majestic monitoring:** The addon now independently monitors the health of each OpenIPC camera. It checks not only ping but also the availability of the RTSP port (554) and, most importantly, analyzes the metrics of the `majestic` process itself (temperature, FPS, memory) obtained via `/metrics`.
*   **Автоперезапуск:** Если `majestic` перестает отвечать (например, падает веб-интерфейс или пропадает RTSP), аддон автоматически перезапустит его через SSH после 3 неудачных проверок.
    **Auto-restart:** If `majestic` becomes unresponsive (e.g., the web interface crashes or RTSP is lost), the addon will automatically restart it via SSH after 3 failed checks.
*   **Ежедневные отчеты в Telegram:** Каждый день в 21:00 вы будете получать подробный отчет о состоянии всех ваших камер: сколько здоровых, сколько с предупреждениями, сколько было перезапусков и общая статистика по записям и снимкам.
    **Daily Telegram reports:** Every day at 9:00 PM, you will receive a detailed report on the status of all your cameras: how many are healthy, how many have warnings, how many restarts occurred, and overall statistics on recordings and snapshots.

### 🗄️ **Управление снимками / Snapshot Management**
*   **Реальные снимки:** Страница "Снимки" теперь показывает реальные файлы из папки `/config/www/snapshots`, а не тестовые данные.
    **Real snapshots:** The "Snapshots" page now displays actual files from the `/config/www/snapshots` folder, not mock data.
*   **Удобный просмотр:** Добавлен просмотр в модальном окне с детальной информацией (время, камера, тип, размер) и возможностью скачать или отправить снимок в Telegram.
    **Convenient viewing:** Added a modal view with detailed information (time, camera, type, size) and the ability to download or send the snapshot to Telegram.

### ⚙️ **Улучшения интерфейса / UI Improvements**
*   **Системная вкладка:** В настройках камер (`/config`) появилась новая вкладка "Система", где можно изменить лимит одновременных записей, применить настройки ко всем камерам сразу и увидеть статистику.
    **System tab:** A new "System" tab has appeared in the camera settings (`/config`), where you can change the limit for simultaneous recordings, apply settings to all cameras at once, and see statistics.
*   **Временные метки в архиве:** При просмотре видео на шкале времени теперь отображается абсолютное время записи, что позволяет точно понять, когда произошло событие.
    **Timeline timestamps in archive:** When viewing a video, the absolute recording time is now displayed on the timeline, allowing you to know exactly when an event occurred.

---

## 🚧 **Планы на будущее / Future Plans**

*   **Расширенная детекция объектов (Object Detection):** В следующих релизах мы планируем добавить настоящую детекцию людей, автомобилей и других объектов на базе ИИ.
    **Advanced Object Detection:** In future releases, we plan to add real AI-based detection of people, cars, and other objects.
*   **Распознавание номеров (LPR):** Текущие сенсоры для номеров являются заглушками. Полноценная поддержка LPR появится позже.
    **License Plate Recognition (LPR):** The current license plate sensors are placeholders. Full LPR support will come later.


---

## 📦 Установка / Installation

### 1. Аддон OpenIPC Bridge (Требуется для новых функций / Required for new features)
1.  Перейдите в Supervisor → Add-on Store.
2.  Нажмите на три точки (⋮) в правом верхнем углу и выберите "Repositories".
3.  Добавьте репозиторий: `https://github.com/OpenIPC/hass`
4.  Найдите и установите аддон **OpenIPC Bridge**.
5.  После установки аддон будет доступен по адресу `http://[IP-АДРЕС-HA]:5000`

### 2. Интеграция OpenIPC Camera
*   **Через HACS (рекомендуется):**
    1.  Откройте HACS → Integrations → ⋮ → Custom repositories.
    2.  Добавьте `https://github.com/OpenIPC/hass` с категорией "Integration".
    3.  Найдите "OpenIPC Camera" и установите.
    4.  Перезапустите Home Assistant.
*   **Вручную:**
    Скопируйте папку `custom_components/openipc` в директорию `/config/custom_components/` и перезапустите HA.

---

## 🤝 Как помочь проекту / How to Contribute

*   ⭐ Поставьте звезду на GitHub / Star us on GitHub
*   🐛 Сообщайте об ошибках в Issues / Report bugs in Issues
*   📝 Улучшайте документацию / Improve documentation
*   🔧 Отправляйте Pull Request'ы / Submit Pull Requests

---

## 📜 Лицензия / License

MIT License

**OpenIPC Community - делаем умный дом доступнее! / making smart homes accessible!** 🚀

🤝 Contributing
⭐ Star us on GitHub

🐛 Report issues in Issues

📝 Improve documentation

🔧 Submit Pull Requests

📜 License
MIT License

OpenIPC Community - making smart homes accessible! 🚀
