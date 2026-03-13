
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


🤝 Contributing
⭐ Star us on GitHub

🐛 Report issues in Issues

📝 Improve documentation

🔧 Submit Pull Requests

📜 License
MIT License

OpenIPC Community - making smart homes accessible! 🚀
