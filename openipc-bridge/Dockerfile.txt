ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache \
    ffmpeg \
    curl \
    py3-pip \
    py3-pillow \
    py3-aiohttp \
    py3-aiofiles \
    py3-numpy \
    zbar \
    py3-zbar \
    python3 \
    python3-dev \
    py3-virtualenv \
    gcc \
    musl-dev \
    linux-headers \
    libffi-dev \
    openssl-dev \
    cmake \
    make \
    py3-opencv \
    py3-gtts \
    py3-pyzbar \
    py3-flask \
    py3-jinja2 \
    py3-werkzeug \
    py3-markupsafe \
    py3-itsdangerous \
    py3-requests \
    py3-click \
    py3-six \
    py3-yaml \
    openssh-client \
    sshpass

# Create virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install additional Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    requests \
    pyyaml \
    psutil \
    paramiko \
    cryptography \
    bcrypt \
    pynacl \
    schedule

# Copy all necessary files
COPY run.sh /
COPY server.py /app/
COPY stream_monitor.py /app/
COPY recording_api.py /app/
COPY config_manager.py /app/
COPY camera_monitor.py /app/
COPY daily_reporter.py /app/
COPY tts_generate_openipc.sh /app/
COPY tts_generate.sh /app/
COPY tts_generate_rhvoice.sh /app/
COPY check_modules.py /app/
COPY templates/ /app/templates/

# Make scripts executable
RUN chmod a+x /run.sh && \
    chmod a+x /app/tts_generate_openipc.sh && \
    chmod a+x /app/tts_generate.sh && \
    chmod a+x /app/tts_generate_rhvoice.sh && \
    chmod a+x /app/check_modules.py && \
    chmod a+x /app/stream_monitor.py && \
    chmod a+x /app/recording_api.py && \
    chmod a+x /app/config_manager.py && \
    chmod a+x /app/camera_monitor.py && \
    chmod a+x /app/daily_reporter.py

# Create necessary directories
RUN mkdir -p /config/www/recordings && \
    mkdir -p /config/www/snapshots && \
    mkdir -p /config/www/exports && \
    mkdir -p /config/openipc_backups && \
    mkdir -p /tmp/hls

# Set permissions
RUN chmod -R 777 /tmp/hls && \
    chmod -R 777 /config/www

# Create working directory
WORKDIR /app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

CMD [ "/run.sh" ]