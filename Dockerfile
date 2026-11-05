# One image for the one Render service: the static website, the API and the evaluator
# worker, run under kavren_api.supervise. Testnet only. No secrets are baked in: keys
# come from the host's environment at runtime.

# ---- 1. build the static website ------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
COPY docs/ATTACKS.md /src/docs/ATTACKS.md
# Public values compiled into the site (same as web/.env.example; never keys)
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_NETWORK=testnet \
    NEXT_PUBLIC_CONTRACT_ID=kavren.lahd-kavren.testnet \
    NEXT_PUBLIC_DEFAULT_AGENT=buyer-agent.lahd-kavren.testnet \
    NEXT_PUBLIC_API_URL=same-origin \
    NEXT_PUBLIC_RPC_URLS=
RUN npm test && npm run build

# ---- 2. API + evaluator worker ----------------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY kavren_core/ kavren_core/
COPY api/ api/
COPY agents/ agents/
# agents without its own deps: the evaluator only re-runs its deterministic cleaning code (attest.py)
RUN pip install ./kavren_core ./api && pip install --no-deps ./agents
COPY --from=web /src/web/out /app/web
# Everything that must survive restarts lives on the persistent disk at /var/data
ENV WEB_DIR=/app/web \
    HOST=0.0.0.0 \
    PORT=10000 \
    DATABASE_URL=sqlite:////var/data/kavren.db \
    STORAGE_DIR=/var/data/files \
    BACKUP_DIR=/var/data/backups \
    FILE_RETENTION_DAYS=7 \
    KAVREN_CONTRACT_ID=kavren.lahd-kavren.testnet
EXPOSE 10000
CMD ["python", "-m", "kavren_api.supervise"]
