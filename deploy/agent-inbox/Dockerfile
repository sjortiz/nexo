# Agent Inbox at one pinned commit (the upstream repo is archived). Needs network at build
# time only: git for the source, npm for the packages, Google Fonts for the page font.
FROM node:22.12.0-alpine3.21
ARG AGENT_INBOX_REF=f1616f3e7998e7e4fd574545b9558554d88d9c49
RUN apk add --no-cache git \
    && git init /app && cd /app \
    && git fetch --depth 1 https://github.com/langchain-ai/agent-inbox.git "$AGENT_INBOX_REF" \
    && git checkout --detach FETCH_HEAD
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && yarn install --frozen-lockfile && yarn build
USER node
EXPOSE 3000
CMD ["yarn", "start", "-H", "0.0.0.0", "-p", "3000"]
