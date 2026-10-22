# Базовый образ можно подменить (например, на локальный кэш без доступа к Docker Hub):
#   docker compose build --build-arg PYTHON_IMAGE=my-registry/python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
USER root

# Установка системных зависимостей (FFmpeg, curl); шаг пропускается, если базовый образ их уже содержит
RUN if command -v ffmpeg >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then \
        echo "ffmpeg and curl already present"; \
    else \
        apt-get update && apt-get install -y --no-install-recommends \
            ffmpeg \
            curl \
            ca-certificates \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# Кэширование слоя зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода проекта
COPY app/ ./app/
COPY static/ ./static/
COPY run.py run_bot.py ./

# Непривилегированный пользователь и директории для персистентных данных и медиа
RUN (id app >/dev/null 2>&1 || useradd --uid 1000 --create-home --shell /usr/sbin/nologin app) \
    && mkdir -p /app/data /app/downloads \
    && chown -R app:app /app
USER app

# Переменные окружения по умолчанию
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
