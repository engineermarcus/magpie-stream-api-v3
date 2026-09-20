FROM python:3.11-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN pip install playwright playwright-stealth urllib3 --no-cache-dir && \
    python3 -m playwright install chromium && \
    python3 -m playwright install-deps chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY app.py .

EXPOSE 8888

CMD ["python3", "-u", "app.py"]
