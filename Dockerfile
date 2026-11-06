# Strixhaven Companion - web app image.
# Books, database and extracted text live in the /data volume, never in the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    OLLAMA_URL=http://ollama:11434

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY ingest ./ingest
COPY eval ./eval
COPY sources.yaml README.md ./

# Run as an unprivileged user; /data must be writable by it (Unraid: PUID/PGID 99/100 also works).
RUN useradd --create-home --uid 1000 companion \
    && mkdir -p /data/sources \
    && chown -R companion:companion /data /app
USER companion

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers: correct client IPs/https behind SWAG.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
