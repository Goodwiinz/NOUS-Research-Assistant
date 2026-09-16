# syntax=docker/dockerfile:1
# Development-only frontend image. Build from the repository root so pnpm can
# resolve the frontend as part of the canonical workspace lockfile.

ARG NODE_VERSION=24
FROM node:${NODE_VERSION}-alpine AS base

RUN apk add --no-cache curl libc6-compat && \
    corepack enable && \
    corepack prepare pnpm@10.18.2 --activate

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json frontend/package.json
COPY packages/chat-runtime/package.json packages/chat-runtime/package.json
COPY tests/e2e/package.json tests/e2e/package.json

FROM base AS dependencies

RUN pnpm install --frozen-lockfile --filter multimodal-rag-frontend...

FROM dependencies AS development

COPY frontend/ frontend/
COPY packages/chat-runtime/ packages/chat-runtime/

WORKDIR /app/frontend

ENV NODE_ENV=development
ENV NEXT_TELEMETRY_DISABLED=1

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3000/ || exit 1

CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]
