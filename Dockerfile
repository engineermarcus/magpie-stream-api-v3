FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

RUN pip install playwright-stealth urllib3 --no-cache-dir

COPY app.py .

EXPOSE 8888

CMD ["python3", "app.py"]
