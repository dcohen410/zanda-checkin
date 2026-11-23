# Zanda Checkin service: admin portal, check-in portal and notifier API in one container.
# Base image pinned by digest (Dependabot proposes updates).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
# Every package is checked against the hashes in requirements.lock (regenerate with pip-compile).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock

COPY zanda_checkin ./zanda_checkin

RUN useradd --system --uid 10001 --home /app zc \
 && mkdir -p /data && chown zc /data
USER zc
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"

# One process on purpose: live notifications are broadcast in memory.
CMD ["sh", "-c", "python -m zanda_checkin --host 0.0.0.0 --port ${PORT}"]
