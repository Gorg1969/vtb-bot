# ============================================================
# Dockerfile для vtb-bot (Bothost)
# ВРЕМЕННО БЕЗ YOLO — для стабильной работы бота
# Закраску добавим позже через отдельный сервис
# ============================================================

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    tzdata \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 \
    fonts-liberation fonts-dejavu-core sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        Flask==3.0.0 \
        requests==2.31.0 \
        beautifulsoup4==4.12.2 \
        playwright==1.40.0 \
        Pillow==10.1.0 \
        openpyxl==3.1.2 \
        pytz==2023.3 \
        python-dotenv==1.0.0 && \
    rm -rf /root/.cache/pip /tmp/* /var/tmp/*

RUN playwright install chromium

COPY . .

RUN mkdir -p /app/data /app/data/VTB_Объявления /app/logs

EXPOSE 3000

CMD ["python", "app.py"]
