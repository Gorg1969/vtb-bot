# ============================================================
# Dockerfile для vtb-bot (Bothost)
# Образ: python:3.11-slim + Chromium (Playwright) + OpenCV (YOLO)
# ============================================================

FROM python:3.11-slim

# === Переменные окружения ===
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# === Системные зависимости ===
# - для Playwright/Chromium
# - для OpenCV (YOLO)
# - утилиты
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    tzdata \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    fonts-liberation \
    fonts-dejavu-core \
    sqlite3 \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# === Рабочая папка ===
WORKDIR /app

# === Зависимости Python ===
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# === Установка Chromium для Playwright ===
RUN playwright install chromium

# === Копируем код ===
COPY . .

# === Создаём папки для данных ===
RUN mkdir -p /app/data /app/data/VTB_Объявления /app/logs

# === Открываем порт ===
EXPOSE 3000

# === Точка входа ===
CMD ["python", "app.py"]
