#!/bin/bash
set -e

cd /data/GitHub/YOUTUBE

# Initial generation of latest_videos.html
echo "[$(date)] Initial video fetch..."
python3 youtube_follow.py --generate-only || true

# Start cron daemon
cron

# Start the HTTP server
echo "[$(date)] Starting server on port 8081..."
exec python3 youtube_follow.py --serve
