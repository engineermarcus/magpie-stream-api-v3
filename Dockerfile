FROM python:3.11-slim

WORKDIR /app

# Install system deps for chromium
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install python deps
RUN pip install playwright playwright-stealth urllib3 --no-cache-dir

# Install chromium + its deps
RUN python3 -m playwright install chromium
RUN python3 -m playwright install-deps chromium

# Copy app
COPY app.py .

EXPOSE 8888

CMD ["python3", "app.py"]
