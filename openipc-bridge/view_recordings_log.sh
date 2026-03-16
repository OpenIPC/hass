#!/bin/bash
echo "========================================="
echo "📹 Recording Debug Log"
echo "========================================="
echo

echo "=== Recording Directory Contents ==="
ls -la /config/www/recordings/
echo

echo "=== Write Test ==="
touch /config/www/recordings/test.txt && echo "✅ Can write" || echo "❌ Cannot write"
rm -f /config/www/recordings/test.txt
echo

echo "=== Disk Space ==="
df -h /config
echo

echo "=== Recording Debug Log ==="
if [ -f /config/recording_debug.log ]; then
    tail -50 /config/recording_debug.log
else
    echo "Log file not found"
fi
echo

echo "=== Active Recordings ==="
curl -s http://localhost:5000/api/debug/recordings | python3 -m json.tool
echo

echo "========================================="