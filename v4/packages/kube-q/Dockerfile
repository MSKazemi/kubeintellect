# ── Stage 1: Build Next.js ────────────────────────────────────────────────────
FROM node:20-alpine AS web-builder

# node-pty requires native compilation
RUN apk add --no-cache python3 make g++

WORKDIR /build/web

COPY web/package*.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM node:20-alpine AS runtime

# Python + kube-q CLI (installed from PyPI — always the released version)
RUN apk add --no-cache python3 py3-pip make g++ && \
    pip3 install kube-q --break-system-packages

WORKDIR /app

ENV NODE_ENV=production

# Copy built Next.js app and native node modules from builder
COPY --from=web-builder /build/web/package*.json ./
COPY --from=web-builder /build/web/.next          .next
COPY --from=web-builder /build/web/node_modules   node_modules
COPY --from=web-builder /build/web/public         public
COPY --from=web-builder /build/web/server.mjs     server.mjs
COPY --from=web-builder /build/web/pty-server.mjs pty-server.mjs

EXPOSE 3000

# Env vars injected at runtime by the hosting platform — no .env file baked in.
# Required: KUBE_Q_URL, KUBE_Q_API_KEY
# Optional: KUBE_Q_MODEL, KUBE_Q_USER_NAME, PORT

CMD ["node", "server.mjs"]
