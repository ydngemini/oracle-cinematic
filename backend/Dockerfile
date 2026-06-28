FROM python:3.12-slim

# Build argument allows pinning a custom pip index (e.g. a private PyPI mirror).
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

# Install dependencies in a separate layer so rebuilds skip this step when
# only source code changes (requirements.txt changes less often than .py files).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium — required by the browser-based state harvesters (e.g.
# Maryland SDAT, harvesters/md_sdat_ingest.py) so the parcel-harvest national
# pass covers portals that have no plain HTTP/API export. Installed as ROOT
# (--with-deps pulls the apt system libraries Chromium needs) into a shared path
# that the non-root uid-1001 runtime user can read+execute. PLAYWRIGHT_BROWSERS_PATH
# is an ENV so it applies at both build (install) and runtime (launch). This is a
# heavy layer (~apt libs + ~150MB Chromium) but cached unless requirements change.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

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
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
