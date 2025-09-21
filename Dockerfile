# Dockerfile
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_WORKERS=1 \
    OMP_NUM_THREADS=1 \
    OPENCV_LOG_LEVEL=ERROR

# Системные либы для opencv-python-headless и numpy/scipy
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxext6 libxrender1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Зависимости отдельно (кэш слоёв) ----
# Если у тебя уже есть requirements.txt в корне проекта — супер.
# Если нет, см. пример ниже.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip wheel setuptools \
 && pip install -r /app/requirements.txt

# ---- Код приложения ----
COPY . /app

# Непривилегированный пользователь (безопаснее)
RUN useradd -ms /bin/bash appuser \
 && mkdir -p /tmp \
 && chown -R appuser:appuser /app /tmp
USER appuser

EXPOSE 8899

# Важно: один воркер из-за WebSocket-состояния
# Если модуль у тебя app/main_ws.py => app.main_ws:app
CMD ["uvicorn", "main_ws:app", "--host", "0.0.0.0", "--port", "8899", "--workers", "1", "--ws", "websockets"]