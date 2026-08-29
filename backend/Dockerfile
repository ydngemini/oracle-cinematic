FROM python:3.12-slim

# Build argument allows pinning a custom pip index (e.g. a private PyPI mirror).
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=180
ARG PIP_RETRIES=10
ARG INSTALL_TEST_DEPS=false

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

# Non-root runtime user — the app never needs to write outside /app.
RUN useradd -r -u 1001 oracle && chown -R oracle:oracle /app
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
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30"]
