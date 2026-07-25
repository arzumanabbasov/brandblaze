FROM node:22-bookworm-slim AS web-build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY app ./app
COPY public ./public
COPY next.config.ts postcss.config.mjs tsconfig.json vite.config.ts ./
RUN npm exec vite build


FROM node:22-bookworm-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOST=127.0.0.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        gettext-base \
        nginx \
        python3 \
        python3-pip \
        python3-venv \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY --from=web-build /app/.output ./.output
COPY api ./api
COPY docker ./docker

RUN chmod +x /app/docker/start.sh \
    && mkdir -p /app/.runtime/runs /var/log/supervisor

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD curl --fail http://127.0.0.1:${PORT:-10000}/health || exit 1

CMD ["/app/docker/start.sh"]
