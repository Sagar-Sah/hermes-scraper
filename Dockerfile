FROM node:22-bookworm-slim AS dashboard
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY backend/crawler/build_info.py /build/backend/crawler/build_info.py
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    ATLAS_DATA_DIR=/data
WORKDIR /app/backend
COPY backend/ ./
RUN pip install -r requirements.lock \
    && pip install --no-deps . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 atlas \
    && useradd --uid 10001 --gid atlas --create-home atlas \
    && mkdir -p /data \
    && chown -R atlas:atlas /data /app \
    && chmod -R a+rX /ms-playwright
COPY --from=dashboard /build/frontend/dist /app/frontend/dist
USER atlas
EXPOSE 18877
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18877/api/v1/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "crawler.app:app", "--host", "0.0.0.0", "--port", "18877", "--workers", "1"]
