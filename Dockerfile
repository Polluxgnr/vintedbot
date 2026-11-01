# Vinted Hunter - runs on x86 (PC, NUC, ThinkCentre) and ARM (Raspberry Pi, Mac mini via Docker).
# Must run on a HOME connection: Vinted blocks datacenter IPs (cloud hosting gets HTTP 429 after a few requests).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/hunter.db TZ=Europe/Paris

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY vinted_hunter ./vinted_hunter
COPY config ./config

RUN useradd -m hunter && mkdir -p /data && chown hunter /data
USER hunter
VOLUME ["/data"]

# The bot holds a localhost port while running (single-instance lock): if nothing listens, it is dead.
HEALTHCHECK --interval=2m --timeout=10s --start-period=1m --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 47821), 5)" || exit 1

CMD ["python", "-m", "vinted_hunter"]
