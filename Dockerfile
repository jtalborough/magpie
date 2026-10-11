FROM python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LED_CONFIG=/app/config/container.json \
    BUILD_INFO_PATH=/app/build.json

WORKDIR /app

COPY pyproject.toml README.md requirements-container.lock ./
COPY src ./src
COPY config ./config

RUN python -m pip install --no-cache-dir -r requirements-container.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation .

RUN mkdir -p /data

RUN groupadd --system --gid 10001 magpie \
    && useradd --system --uid 10001 --gid magpie --home-dir /app --shell /usr/sbin/nologin magpie \
    && chown -R magpie:magpie /data

ARG BUILD_COMMIT=container-dev
ARG BUILD_BRANCH=container
ARG BUILT_AT=image-build

RUN printf '{"version":"0.1.0","commit":"%s","branch":"%s","dirty":false,"built_at":"%s","source":"container"}\n' \
    "$BUILD_COMMIT" "$BUILD_BRANCH" "$BUILT_AT" > /app/build.json

VOLUME ["/data"]
EXPOSE 8765

USER magpie

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/ready', timeout=5)"]

CMD ["python", "-m", "searchable_storage_led.api"]
