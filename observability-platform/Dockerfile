# observability-platform/Dockerfile — Neoh AWS observability dashboard image.
#
# Mirrors oracle-app: (1) build the Vite SPA, baking VITE_* browser-public vars
# in at build time; (2) serve the static dist via nginx on non-root :8080. In
# prod the ALB routes this dashboard's host to it, and that host's /api/*, /ws,
# /auth/* to the backend target group — so the SPA calls the API SAME-ORIGIN
# (no CORS). VITE_API_BASE / VITE_WS_URL are passed to point at that host.

# ---- build stage ----------------------------------------------------------
FROM node:24.18.0-alpine AS build
WORKDIR /app

# Install deps against the lockfile first so this layer caches across source edits.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY . .

# Browser-PUBLIC build args (NOT secrets — they ship in the JS bundle). Vite
# reads these from the build environment; blank values select same-origin ALB
# routing in production.
ARG VITE_API_BASE=""
ARG VITE_WS_URL=""
ENV VITE_API_BASE=${VITE_API_BASE}
ENV VITE_WS_URL=${VITE_WS_URL}

RUN npm run build

# ---- serve stage ----------------------------------------------------------
FROM nginx:1.30.4-alpine AS serve

# Drop nginx's default site; install our SPA config (listens on 8080, non-root).
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/observability.conf

COPY --from=build /app/dist /usr/share/nginx/html

# Run unprivileged. nginx:alpine ships an "nginx" user; give it the runtime dirs
# it writes to so it can start without root.
RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx /var/run/nginx.pid /var/cache/nginx /usr/share/nginx/html
USER nginx

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
