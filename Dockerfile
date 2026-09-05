FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRONTDESK_DATA_DIR=/var/lib/frontdesk

WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir . \
    && mkdir -p /var/lib/frontdesk \
    && chown -R 10001:0 /var/lib/frontdesk /app

USER 10001
VOLUME ["/var/lib/frontdesk"]
EXPOSE 8765 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=3).read(); urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"

CMD ["python", "server.py", "--host", "0.0.0.0"]
