FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    fonts-liberation libglib2.0-0 libnss3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    playwright \
    playwright-stealth \
    urllib3

RUN playwright install chromium --with-deps

COPY stream-api.py .

EXPOSE 8888

CMD ["python3", "stream-api.py", "--host", "0.0.0.0", "--port", "8888"]
