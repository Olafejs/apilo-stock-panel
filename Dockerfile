FROM python:3.12-slim

ARG APP_VERSION=1.14.1
LABEL org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 \
    APP_HOST=127.0.0.1 \
    APP_PORT=5080 \
    APILO_DB_PATH=/app/data/apilo.sqlite3

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/logs /app/static/thumbs \
    && chown -R app:app /app /home/app

EXPOSE 5080

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sys, urllib.request; port = os.environ.get('APP_PORT', '5080'); response = urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3); sys.exit(0 if response.status == 200 else 1)"

ENTRYPOINT ["python", "docker-entrypoint.py"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
