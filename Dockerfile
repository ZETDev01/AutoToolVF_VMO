FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VINFAST_WEB_HOST=0.0.0.0 \
    VINFAST_ROBOT_HOST=0.0.0.0 \
    VINFAST_ROBOT_PORT=9000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY requirements-audio.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install -r requirements-audio.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8080 9000

CMD ["./docker-entrypoint.sh"]
