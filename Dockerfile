# Dockerfile für NeiroFitnessApp - Production Ready
# Optimiert für Render.com Deployment
FROM python:3.11-slim AS base

# ========== Environment Variables ==========
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Limit threads to prevent memory issues
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    # OpenCV/YOLO settings
    OPENCV_LOG_LEVEL=ERROR \
    # Python memory optimization
    MALLOC_TRIM_THRESHOLD_=100000 \
    # Render.com uses PORT env variable
    PORT=8000

# ========== System Dependencies ==========
RUN apt-get update && apt-get install -y --no-install-recommends \
    # OpenCV dependencies
    libgl1-mesa-dri \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libgthread-2.0-0 \
    # Video processing
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    # For health checks
    curl \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && apt-get clean

# ========== Create Non-Root User ==========
RUN useradd -ms /bin/bash appuser && \
    mkdir -p /app/uploads/neirofitness/output && \
    chown -R appuser:appuser /app

USER appuser
WORKDIR /app

# ========== Install Python Dependencies ==========
COPY --chown=appuser:appuser requirements.txt /app/requirements.txt
RUN pip install --user --upgrade pip wheel setuptools && \
    pip install --user -r /app/requirements.txt && \
    # Clean pip cache
    rm -rf ~/.cache/pip

# ========== Copy Application Code ==========
COPY --chown=appuser:appuser . .

# ========== Pre-load YOLO Model ==========
# This ensures the model is cached in the image
RUN python -c "from ultralytics import YOLO; YOLO('app/models/bestv2.pt')" || true

# ========== Health Check ==========
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# ========== Expose Port ==========
EXPOSE ${PORT}

# ========== Start Application ==========
# Use exec form for proper signal handling
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 30"]
