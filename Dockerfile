# Dockerfile для NeiroFitnessApp
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_WORKERS=1 \
    OMP_NUM_THREADS=1 \
    OPENCV_LOG_LEVEL=ERROR

# Системные библиотеки для opencv-python-headless
RUN apt-get update && apt-get install -y \
    libgl1-mesa-dri \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libgthread-2.0-0 \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и устанавливаем зависимости
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip wheel setuptools \
 && pip install -r /app/requirements.txt

# Копируем код приложения
COPY . /app

# Создаем директории для временных файлов
RUN mkdir -p /tmp/neirofitness /tmp/neirofitness/output

# Создаем непривилегированного пользователя
RUN useradd -ms /bin/bash appuser \
 && chown -R appuser:appuser /app /tmp/neirofitness
USER appuser

# Открываем порт
EXPOSE 8000

# Запускаем приложение
CMD ["python", "main.py"]