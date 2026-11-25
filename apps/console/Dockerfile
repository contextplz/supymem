# syntax=docker/dockerfile:1.19
ARG NODE_BASE=public.ecr.aws/docker/library/node:22.23.3-bookworm-slim
FROM ${NODE_BASE} AS local
WORKDIR /app
RUN npm install -g pnpm@10.11.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY --exclude=carry-static . .
ENV NEXT_TELEMETRY_DISABLED=1

FROM ${NODE_BASE} AS builder
WORKDIR /app
RUN npm install -g pnpm@10.11.0
COPY --from=local /app/node_modules ./node_modules
# Carried releases affect runtime continuity only, not Next build inputs.
# Keep every other source/config input; runner COPY below still receives chunks.
COPY --exclude=carry-static . .
RUN mkdir -p public
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* is inlined at `next build`. This is the declared parity exception
# from plan §6 (docs/company-os/101). It goes away when the console fetches
# /api/config at runtime for the gateway URL rather than baking it in.
ARG NEXT_PUBLIC_GATEWAY_URL=http://localhost:3200
ARG SUPYMEM_MODE
ARG CORE_URL
ENV NEXT_PUBLIC_GATEWAY_URL=$NEXT_PUBLIC_GATEWAY_URL \
    SUPYMEM_MODE=$SUPYMEM_MODE \
    CORE_URL=$CORE_URL
RUN pnpm build

FROM ${NODE_BASE} AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3100 \
    HOSTNAME=0.0.0.0
RUN groupadd --system --gid 10001 nodejs \
    && useradd --system --uid 10001 --gid nodejs --home /app --shell /usr/sbin/nologin nextjs
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
# Previous releases' hashed chunks (filled by hotfix-console.yml; empty otherwise) so
# open sessions on an older build don't 404 mid-navigation. Current build copied after.
COPY --chown=nextjs:nodejs carry-static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3100
CMD ["node", "server.js"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=12 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3100)+'/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

FROM runner AS runner-with-notices
COPY --from=release_source LICENSE NOTICE /app/

FROM runner AS default-runner
