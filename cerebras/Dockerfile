FROM python:3.12-slim
WORKDIR /srv/cerebras
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY connectors.yaml .
ENV PYTHONUNBUFFERED=1
EXPOSE 8141
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD ["python", "/srv/cerebras/app/healthcheck.py"]
# host networking: bind loopback only — Caddy proxies 127.0.0.1:8141
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8141", "--workers", "1"]
