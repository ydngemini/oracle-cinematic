# oracle-app/Dockerfile — frontend production image.
#
# Two stages: (1) build the Vite SPA, baking the VITE_* browser-public vars in at
# build time; (2) serve the static dist via nginx on a non-root port. Azure uses
# separate public Container Apps for the SPA and API, so release builds must pass
# their explicit HTTPS/WSS origins as VITE_API_BASE and VITE_WS_URL. A same-origin
# empty base is only valid behind an ingress that actually proxies /api and /ws.

# ---- build stage ----------------------------------------------------------
# Base image pinned by DIGEST (resolved 2026-09-25), with the tag kept only so
# a human can read it. A tag like `3.12-slim` is repointed upstream whenever
# the image is rebuilt, so every build silently picked up whatever it meant
# that day — builds were not reproducible. Pinned, the same commit builds the
# same base. The cost is that upstream patches no longer arrive by accident:
# the release job's Trivy scan fails on a fixable CRITICAL, and that is the
# signal to refresh this digest (docs/supply-chain-policy.md).
FROM node:26.12.0-alpine@sha256:6f08a523443ddedf52941874c203475b0ad503e859ad70cb26ab8be9a517dbd5 AS build
WORKDIR /app

# Install deps against the lockfile first so this layer caches across source edits.
COPY package.json package-lock.json* ./
# Install the committed stable dependency graph exactly. Audit metadata and
# funding lookups are excluded from the image build; release gates run them
# separately before image publication.
RUN npm ci --no-audit --no-fund

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
FROM nginx:1.31.7-alpine@sha256:892bb2152464f0c5d8972593a5f5afaf8054552580b699d95f8ff4af08c2d33d AS serve

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
