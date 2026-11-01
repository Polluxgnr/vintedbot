FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/hunter.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vinted_hunter ./vinted_hunter
COPY config ./config
RUN useradd -m hunter && mkdir /data && chown hunter /data
USER hunter
VOLUME ["/data"]
CMD ["python", "-m", "vinted_hunter"]
