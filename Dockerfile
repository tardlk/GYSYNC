FROM python:3.14-slim AS builder
WORKDIR /build
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip wheel --index-url "$PIP_INDEX_URL" --no-cache-dir --require-hashes -r requirements.lock --wheel-dir /wheels
COPY pyproject.toml LICENSE ./
COPY guangya ./guangya
RUN pip wheel --index-url "$PIP_INDEX_URL" --no-cache-dir --no-deps . --wheel-dir /wheels

FROM python:3.14-slim
LABEL org.opencontainers.image.title="Guangya Organizer" \
      org.opencontainers.image.source="https://github.com/tardlk/GYSYNC" \
      org.opencontainers.image.description="光鸭上传、Telegram 转存、本地与云盘媒体整理" \
      org.opencontainers.image.licenses="GPL-3.0-only"
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels guangya-organizer && rm -rf /wheels \
    && mkdir -p /config /downloads /media && chown -R 1000:1000 /config /downloads /media
COPY LICENSE UPSTREAM.json PLUGIN_UPSTREAM.json /app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/config PORT=8796 HOME=/tmp
USER 1000:1000
EXPOSE 8796
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8796/health', timeout=3)"
CMD ["python", "-m", "guangya.app"]
