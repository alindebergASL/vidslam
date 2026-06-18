# Multi-stage so a single Dockerfile serves both dev (volume-mounted, HMR)
# and prod (built, immutable, `next start`). Picked by `--target` in
# docker-compose.* — `dev` is the default to preserve `make up` behavior.

# ---------- dev: lightweight, mounted source, HMR ----------
FROM node:22-alpine AS dev
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

# ---------- builder: install all deps + compile the Next bundle ----------
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
# NEXT_PUBLIC_* are inlined at build time. Default to a reasonable in-cluster
# value; the compose file or runtime --build-arg can override.
ARG NEXT_PUBLIC_API_BASE=/api
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
RUN npm run build

# ---------- prod: minimal runtime, runs the built bundle ----------
FROM node:22-alpine AS prod
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/package.json ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.mjs ./next.config.mjs
EXPOSE 3000
# `next start` serves the production build; no source watcher, no HMR.
CMD ["npm", "run", "start"]
