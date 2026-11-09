# Single-container build: Next.js frontend (public port) + FastAPI backend (internal 127.0.0.1:8000).
# Used for one-service hosting such as Render. docker-compose.yml still runs them separately.

FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && npm prune --omit=dev

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NODE_ENV=production \
    BACKEND_URL=http://127.0.0.1:8000 PORT=3000
COPY --from=web /usr/local/bin/node /usr/local/bin/node
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/package.json /app/frontend/package.json
COPY --from=web /web/node_modules /app/frontend/node_modules
COPY --from=web /web/.next /app/frontend/.next
COPY --from=web /web/next.config.mjs /app/frontend/next.config.mjs
COPY deploy/start.sh /app/start.sh
RUN useradd --create-home checkmate && mkdir -p /app/backend/.keys \
    && chown -R checkmate /app/backend && chmod +x /app/start.sh
USER checkmate
EXPOSE 3000
CMD ["/app/start.sh"]
