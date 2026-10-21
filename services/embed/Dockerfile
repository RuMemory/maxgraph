FROM python:3.12-slim

# fastembed кэширует модели в FASTEMBED_CACHE_PATH (HF_HOME — запасной вариант для huggingface_hub)
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed-cache \
    HF_HOME=/opt/fastembed-cache

WORKDIR /app

# curl нужен HEALTHCHECK'у, в slim его нет
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Предзагрузка модели (~500MB) в кэш внутри образа: контейнеру сеть для старта не нужна.
# RELAY_HMAC_SECRET здесь — dummy только для fail-fast импорта main.py. НИКОГДА не подставлять
# сюда настоящий секрет: значение RUN-команды видно в docker history.
RUN RELAY_HMAC_SECRET=build-only python -c "import main; main.get_model()"

RUN useradd --system --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data /opt/fastembed-cache
USER appuser

EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8788/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8788"]
