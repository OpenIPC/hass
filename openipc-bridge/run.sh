#!/usr/bin/with-contenv bashio
set -e

echo "========================================="
echo "Starting OpenIPC Bridge Addon (HLS version)"
echo "========================================="
echo "$(date): Starting up..."

# Создаем директорию для HLS сегментов
mkdir -p /tmp/hls
chmod 777 /tmp/hls

# Безопасно добавляем системные пути
if [ -d "/usr/lib/python3.12/site-packages" ]; then
    export PYTHONPATH="/usr/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
    echo "$(date): PYTHONPATH set to: $PYTHONPATH"
fi

# Информация о системе
echo "$(date): Python version: $(python3 --version)"
echo "$(date): Python path: $(which python3)"

# Проверяем наличие токена Supervisor
if [ -n "$SUPERVISOR_TOKEN" ]; then
    echo "$(date): ✅ SUPERVISOR_TOKEN available"
else
    echo "$(date): ⚠️ SUPERVISOR_TOKEN not set"
fi

# Проверяем наши скрипты
if [ -f /app/tts_generate_openipc.sh ]; then
    echo "$(date): ✅ OpenIPC TTS script available"
else
    echo "$(date): ❌ OpenIPC TTS script missing"
    exit 1
fi

if [ -f /app/tts_generate.sh ]; then
    echo "$(date): ✅ Beward TTS script available"
else
    echo "$(date): ❌ Beward TTS script missing"
    exit 1
fi

# Запускаем Flask сервер
if [ -f /app/server.py ]; then
    echo "$(date): Starting Flask server on port 5000..."
    cd /app
    python3 server.py &
    FLASK_PID=$!
    echo "$(date): Flask server started with PID: $FLASK_PID"
else
    echo "$(date): ❌ server.py not found!"
    exit 1
fi

echo "$(date): Addon started successfully"
echo "========================================="

# Держим контейнер запущенным
while true; do
    # Проверяем, что ffmpeg процессы не зависли
    echo "$(date): OpenIPC Bridge running... (Flask PID: ${FLASK_PID:-unknown})"
    sleep 60
done