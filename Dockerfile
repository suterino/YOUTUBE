FROM python:3.11-slim

# Install system deps + Node.js 20
RUN apt-get update && apt-get install -y \
    curl \
    cron \
    ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN pip install --no-cache-dir yt-dlp

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Copy crontab
COPY crontab /etc/cron.d/youtube-follow
RUN chmod 0644 /etc/cron.d/youtube-follow && crontab /etc/cron.d/youtube-follow

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /data/GitHub/YOUTUBE

EXPOSE 8081

ENTRYPOINT ["/entrypoint.sh"]
