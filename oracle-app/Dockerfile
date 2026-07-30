# oracle-app/Dockerfile — frontend production image.
#
# Two stages: (1) build the Vite SPA, baking the VITE_* browser-public vars in at
# build time; (2) serve the static dist via nginx on a non-root port. Azure uses
# separate public Container Apps for the SPA and API, so release builds must pass
# their explicit HTTPS/WSS origins as VITE_API_BASE and VITE_WS_URL. A same-origin
# empty base is only valid behind an ingress that actually proxies /api and /ws.

# ---- build stage ----------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Install deps against the lockfile first so this layer caches across source edits.
COPY package.json package-lock.json* ./
# React's view-transition build is a pinned canary. Some libraries declare only
# stable React peer ranges even though they are runtime-compatible, so preserve
# the lockfile exactly without asking npm to replace the pinned React build.
RUN npm ci --legacy-peer-deps

COPY . .

# Browser-PUBLIC build args (NOT secrets — they ship in the JS bundle). The Google
# Maps key is restricted by HTTP referrer in the Google console, not kept secret.
ARG VITE_API_BASE=""
ARG VITE_WS_URL=""
ARG VITE_GOOGLE_MAPS_KEY=""
ARG VITE_GOOGLE_MAP_ID=""
ARG VITE_LOCAL_MODELS_ENABLED="false"
ARG VITE_MUNICIPAL_HARVESTS_ENABLED="false"
ENV VITE_API_BASE=${VITE_API_BASE}
ENV VITE_WS_URL=${VITE_WS_URL}
ENV VITE_GOOGLE_MAPS_KEY=${VITE_GOOGLE_MAPS_KEY}
ENV VITE_GOOGLE_MAP_ID=${VITE_GOOGLE_MAP_ID}
ENV VITE_LOCAL_MODELS_ENABLED=${VITE_LOCAL_MODELS_ENABLED}
ENV VITE_MUNICIPAL_HARVESTS_ENABLED=${VITE_MUNICIPAL_HARVESTS_ENABLED}

# Vite reads env from the repo root (envDir:'..'); the build arg above is already
# in-process so it is picked up regardless. Outputs to /app/dist.
RUN npm run build

# ---- serve stage ----------------------------------------------------------
FROM nginx:1.27-alpine AS serve

# Drop nginx's default site; install our SPA config (listens on 8080, non-root).
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/oracle.conf

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
