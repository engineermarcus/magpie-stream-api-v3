FROM python:3.11-slim

WORKDIR /app

RUN pip install playwright playwright-stealth urllib3 --no-cache-dir && \
    python3 -m playwright install chromium --with-deps && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /root/.cache

COPY app.py .

EXPOSE 8888

CMD ["python3", "app.py"]
