# Agent Inbox at one pinned commit (the upstream repo is archived). Needs network at build
# time only: git for the source, npm for the packages, Google Fonts for the page font.
FROM node:22.14.0-alpine3.21
ARG AGENT_INBOX_REF=f1616f3e7998e7e4fd574545b9558554d88d9c49
RUN apk add --no-cache git \
    && git init /app && cd /app \
    && git fetch --depth 1 https://github.com/langchain-ai/agent-inbox.git "$AGENT_INBOX_REF" \
    && git checkout --detach FETCH_HEAD
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# A capped heap keeps the Next.js build inside a small Podman VM (3 GB on the native
# telemetry machine); without it the build is killed for memory.
RUN corepack enable && yarn install --frozen-lockfile \
    && NODE_OPTIONS=--max-old-space-size=1536 yarn build \
    && yarn cache clean  # ~1 GB of package tarballs the running page never reads
USER node
EXPOSE 3000
CMD ["yarn", "start", "-H", "0.0.0.0", "-p", "3000"]
