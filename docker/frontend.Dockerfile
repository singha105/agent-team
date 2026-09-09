# The AgentTeam web UI: built with Vite, served by nginx.

# ---- build -----------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /build

# Lockfile first: npm ci is reproducible and this layer survives source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- runtime ---------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# nginx's own unprivileged image variant would also work; doing it explicitly
# keeps the reason visible: nothing here needs root, and a static file server
# running as root is a gift to anyone who finds a path traversal.
RUN adduser --system --uid 10002 --ingroup nginx agentteam

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

# nginx wants these writable, and they are root-owned in the base image.
RUN chown -R agentteam:nginx /var/cache/nginx /var/run /etc/nginx/conf.d \
    && touch /var/run/nginx.pid && chown agentteam:nginx /var/run/nginx.pid

USER agentteam

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=4s --start-period=8s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
