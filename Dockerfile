FROM node:24-bookworm-slim AS frontend
WORKDIR /build
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY src ./src
COPY public ./public
COPY index.html login.html tsconfig.json vite.config.ts ./
ENV NODE_OPTIONS=--max-old-space-size=512
RUN pnpm build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GRID_DATABASE=/var/lib/grid-studio/live.sqlite3 \
    GRID_CREDENTIAL_KEY_FILE=/var/lib/grid-studio-keys/credential.key
WORKDIR /app
COPY backend/requirements-lock.txt ./backend/requirements-lock.txt
RUN pip install --no-cache-dir -r backend/requirements-lock.txt \
    && groupadd -g 10001 grid-studio \
    && useradd -u 10001 -g grid-studio -d /var/lib/grid-studio -s /usr/sbin/nologin grid-studio \
    && install -d -m 700 -o grid-studio -g grid-studio /var/lib/grid-studio /var/lib/grid-studio-keys
COPY --chown=10001:10001 backend/app ./backend/app
COPY --chown=10001:10001 scripts ./scripts
COPY --chown=10001:10001 deploy ./deploy
COPY --chown=10001:10001 public/favicon.svg ./public/favicon.svg
COPY --from=frontend --chown=10001:10001 /build/dist ./dist
USER 10001:10001
EXPOSE 18473
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:18473/api/auth/session',timeout=3)); assert r['configured'] is True"
CMD ["python", "deploy/container_start.py"]
