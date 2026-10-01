# Base image pinned by DIGEST (resolved 2026-09-25), with the tag kept only so
# a human can read it. A tag like `3.12-slim` is repointed upstream whenever
# the image is rebuilt, so every build silently picked up whatever it meant
# that day — builds were not reproducible. Pinned, the same commit builds the
# same base. The cost is that upstream patches no longer arrive by accident:
# the release job's Trivy scan fails on a fixable CRITICAL, and that is the
# signal to refresh this digest (docs/supply-chain-policy.md).
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# Build argument allows pinning a custom pip index (e.g. a private PyPI mirror).
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=180
ARG PIP_RETRIES=10
ARG INSTALL_TEST_DEPS=false

# Release identity — see GET /version (server.py). A running container has no
# other way to know its own build history; --build-arg is the only place this
# can come from. CI sets these to the real commit SHA, an app version, and the
# build time; a manual local build gets honest "unknown" defaults rather than
# a value that lies by omission.
ARG GIT_SHA=unknown
ARG APP_VERSION=unknown
ARG BUILD_TIMESTAMP=unknown
ENV ORACLE_GIT_SHA=${GIT_SHA} \
    ORACLE_APP_VERSION=${APP_VERSION} \
    ORACLE_BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Large binary wheels (PyAV, ONNX Runtime, Playwright dependencies) can exceed
# pip's short default read timeout on a cold local build. Keep retries bounded
# while allowing slow package mirrors to complete instead of failing startup.
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES}

WORKDIR /app

# Install dependencies in a separate layer so rebuilds skip this step when
# only source code changes (requirements.txt changes less often than .py files).
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_TEST_DEPS" = "true" ]; then pip install --no-cache-dir -r requirements-test.txt; fi

# Playwright + Chromium — required by the browser-based state harvesters (e.g.
# Maryland SDAT, harvesters/md_sdat_ingest.py) so the parcel-harvest national
# pass covers portals that have no plain HTTP/API export. Installed as ROOT
# (--with-deps pulls the apt system libraries Chromium needs) into a shared path
# that the non-root uid-1001 runtime user can read+execute. PLAYWRIGHT_BROWSERS_PATH
# is an ENV so it applies at both build (install) and runtime (launch). This is a
# heavy layer (~apt libs + ~150MB Chromium) but cached unless requirements change.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# ffmpeg — the reconstruction worker samples frames out of a capture video
# before handing images to a provider. Extraction happens here rather than in a
# provider image because every provider's contract is reconstruct(images,
# work_dir): doing it per-provider would mean changing that contract and
# reimplementing it in each backend. One small apt package keeps a phone walk
# video usable everywhere, including the local stub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Amazon RDS global CA bundle — lets connection.py do TLS verify-full against
# Aurora (HARDENING.md §1, rds.force_ssl=1). ORACLE_RDS_CA_BUNDLE points here.
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
RUN chmod a+r /etc/ssl/certs/rds-global-bundle.pem
ENV ORACLE_RDS_CA_BUNDLE=/etc/ssl/certs/rds-global-bundle.pem

# Copy the rest of the backend source. The compose dev setup bind-mounts /app
# over this, so this layer is only meaningful in a production image build.
COPY . .

# Non-root runtime user. It writes to /app and to the media root below.
RUN useradd -r -u 1001 oracle && chown -R oracle:oracle /app

# Durable media storage for captured photos and finished reconstructions.
# Created HERE, owned by the runtime user, because a named volume mounted over
# an empty path inherits that path's ownership from the image — without this the
# volume arrives root-owned, object_storage.is_configured() answers False, and a
# finished reconstruction fails at the last step with "Object storage is not
# configured". Deliberately outside /app: captured homeowner media must never
# land inside the git working tree that compose bind-mounts there.
RUN mkdir -p /var/neoh-media && chown -R oracle:oracle /var/neoh-media

USER oracle

EXPOSE 8000

# Production default. docker-compose.yml overrides this to add --reload.
#
# --timeout-graceful-shutdown is not optional here. Shutdown waits on
# request-level tasks — a WebSocket that never closes among them — and then on a
# dozen sequential stop_*() awaits in server.py's lifespan, none of which is
# bounded. Without a ceiling one stuck task hangs the process forever: observed
# locally as "Waiting for background tasks to complete." with the app serving
# nothing while still listening. On ECS that is a deploy or scale-in that stalls
# while the task stays registered and healthy-looking in the target group.
#
# 30s is longer than any legitimate drain and shorter than the ALB
# deregistration delay, so a task that will not stop is killed before the load
# balancer stops sending it traffic.
# --ws-max-size: uvicorn's default accepts 16 MiB WebSocket frames; the largest
# legitimate frame (an 8,000-character chat message) is under 32 KB. 1 MiB keeps
# one socket from parking 16 MiB per frame in a replica's memory (review WEB-5).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30", "--ws-max-size", "1048576"]
