#!/bin/bash
echo "========================================="
echo "🎬 Запуск записи со всех камер"
echo "========================================="

# Получаем список камер
CAMERAS=$(curl -s http://localhost:5000/api/cameras/status | grep -o '"ip":"[^"]*"' | cut -d'"' -f4)

for CAMERA in $CAMERAS; do
    echo "📹 Запускаю запись на камере: $CAMERA"
    
    curl -X POST http://localhost:5000/api/recording/start \
        -H "Content-Type: application/json" \
        -d "{
            \"camera\": \"$CAMERA\",
            \"duration\": 60,
            \"detect_motion\": true
        }"
    
    echo ""
    sleep 2
done

echo ""
echo "✅ Запись запущена на всех камерах"
echo "========================================="